/**
 * The one reader of cloud.json.
 *
 * Everything installation-specific -- account, domain, bucket, the read allowlist --
 * lives in cloud.json at the repository root, which is NOT committed. This repository
 * is public, so an account id, a domain and three other people's email addresses have
 * no business in it. cloud.example.json documents every key with placeholder values.
 *
 * The mirror of config.py on the Python side: same idea, same rule that a missing or
 * malformed value is a loud error with a fix in it rather than a stack undeployed
 * halfway. Validation happens here, at synth, because the alternative is discovering
 * an empty allowlist after CloudFront has already been told to gate on it.
 */
import * as fs from 'fs';
import * as path from 'path';

export interface CloudConfig {
	accountId: string;
	region: string;
	profile: string;
	domain: string;
	bucket: string;
	captureTz: string;
	googleClientId: string;
	authDomainPrefix: string;
	allowedEmails: string[];
}

const ROOT = path.resolve(__dirname, '../../..');
const CONFIG = path.join(ROOT, 'cloud.json');
const EXAMPLE = path.join(ROOT, 'cloud.example.json');

class ConfigError extends Error {}

function fail(message: string): never {
	throw new ConfigError(
		`cloud.json: ${message}\n  Looked at: ${CONFIG}\n  Every key is documented in ${path.basename(EXAMPLE)}.`,
	);
}

/** IANA zone names, loosely: "Area/Place", optionally "Area/Region/Place". */
const TZ_RE = /^[A-Za-z]+\/[A-Za-z_]+(\/[A-Za-z_]+)?$/;

export function load(): CloudConfig {
	if (!fs.existsSync(CONFIG)) {
		throw new ConfigError(
			`no cloud.json at ${CONFIG}\n  Copy ${path.basename(EXAMPLE)} to cloud.json and edit it.`,
		);
	}
	let raw: Record<string, unknown>;
	try {
		raw = JSON.parse(fs.readFileSync(CONFIG, 'utf-8'));
	} catch (e) {
		fail(`not valid JSON: ${(e as Error).message}`);
	}
	// Same convention as config.example.json: _-prefixed keys are comments, since JSON
	// has none, and a copied-and-edited file must not then fail to load.
	const known = new Set([
		'account_id', 'region', 'profile', 'domain', 'bucket', 'capture_tz',
		'google_client_id', 'auth_domain_prefix', 'allowed_emails',
	]);
	const unknown = Object.keys(raw).filter((k) => !k.startsWith('_') && !known.has(k));
	if (unknown.length) {
		fail(`unknown key(s) ${unknown.join(', ')}\n  Known keys: ${[...known].sort().join(', ')}`);
	}

	const str = (key: string): string => {
		const v = raw[key];
		if (typeof v !== 'string' || v.trim() === '') fail(`${key} must be a non-empty string`);
		return (v as string).trim();
	};

	const accountId = str('account_id');
	if (!/^\d{12}$/.test(accountId)) fail('account_id must be 12 digits');

	const domain = str('domain');
	// A subdomain, not an apex: the whole DNS plan is to delegate a child zone so the
	// parent's MX and TXT records are never touched. An apex here would mean migrating
	// the parent zone, which is a different and much riskier job.
	if (domain.split('.').length < 3) {
		fail(
			`domain ${domain} looks like an apex domain. This deploys into a DELEGATED ` +
				`subdomain (e.g. solar.${domain}) precisely so the parent zone's mail records ` +
				`are never touched.`,
		);
	}

	const bucket = str('bucket');
	if (!/^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/.test(bucket)) {
		fail(`bucket ${bucket} is not a valid S3 bucket name`);
	}

	const captureTz = str('capture_tz');
	if (!TZ_RE.test(captureTz)) {
		fail(
			`capture_tz ${captureTz} is not an IANA zone name like "Pacific/Auckland". ` +
				`An abbreviation such as "NZST" does not carry DST transitions, and the axis ` +
				`labels would be an hour out for half the year.`,
		);
	}

	const emails = raw['allowed_emails'];
	if (!Array.isArray(emails) || emails.some((e) => typeof e !== 'string')) {
		fail('allowed_emails must be an array of strings');
	}

	return {
		accountId,
		region: str('region'),
		profile: str('profile'),
		domain,
		bucket,
		captureTz,
		// Empty until the GCP client exists; auth-stack.ts is what insists on it, so the
		// data and DNS stacks can deploy first.
		googleClientId: typeof raw['google_client_id'] === 'string' ? raw['google_client_id'].trim() : '',
		authDomainPrefix: str('auth_domain_prefix'),
		allowedEmails: (emails as string[]).map((e) => e.trim().toLowerCase()).filter(Boolean),
	};
}

/** Called by the stacks that cannot work without sign-in configured. */
export function requireAuth(cfg: CloudConfig): void {
	if (!cfg.googleClientId) {
		fail(
			'google_client_id is empty, so the site would have no way to sign anyone in.\n' +
				'  Create the OAuth client in the Google Cloud Console first (External consent\n' +
				'  screen; authorized redirect URI https://' + cfg.domain + '/auth/callback).',
		);
	}
	if (!cfg.allowedEmails.length) {
		// An empty allowlist is not "everyone" and not "nobody" by accident -- it is a
		// site that authenticates people and then refuses all of them. Say so at synth.
		fail(
			'allowed_emails is empty. Cognito will authenticate any Google account, so this\n' +
				'  list is what actually gates the site: empty means every visitor signs in\n' +
				'  successfully and is then refused.',
		);
	}
}
