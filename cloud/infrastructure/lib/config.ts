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
	certificateArn: string;
	captureTz: string;
	googleClientId: string;
	authDomainPrefix: string;
	allowedEmails: string[];
	// Only knowable after AuthPoolStack exists, so they arrive by hand afterwards. See
	// auth-stack.ts for why a Lambda@Edge cannot be told them any other way.
	cognitoUserPoolId: string;
	cognitoClientId: string;
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
		'account_id', 'region', 'profile', 'domain', 'bucket', 'certificate_arn',
		'capture_tz', 'google_client_id', 'auth_domain_prefix', 'allowed_emails',
		'cognito_user_pool_id', 'cognito_client_id',
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
	// A subdomain, not an apex. The site is reached by a plain CNAME at whatever
	// registrar holds the parent zone, so the parent's own records -- apex A, MX, TXT --
	// are never touched. A CNAME is illegal at a zone apex (RFC 1034), so an apex here
	// would not merely be riskier, it would not work.
	if (domain.split('.').length < 3) {
		fail(
			`domain ${domain} looks like an apex domain. The site is pointed at CloudFront ` +
				`with a CNAME, which is illegal at a zone apex -- and using a subdomain ` +
				`(e.g. solar.${domain}) is what keeps the parent's mail records untouched.`,
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

	const certificateArn = str('certificate_arn');
	// The certificate is created out of band and referenced by ARN -- see cloud/README.md
	// for why. It must be us-east-1: CloudFront can attach a certificate from nowhere
	// else, and the failure if it is elsewhere is an unhelpful CloudFront error late in
	// the site deploy rather than anything that names the region.
	if (!/^arn:aws:acm:us-east-1:\d{12}:certificate\/[0-9a-f-]+$/.test(certificateArn)) {
		fail(
			`certificate_arn ${certificateArn} is not a us-east-1 ACM certificate ARN. ` +
				`CloudFront can only attach a certificate from us-east-1.`,
		);
	}

	return {
		accountId,
		region: str('region'),
		profile: str('profile'),
		domain,
		bucket,
		certificateArn,
		captureTz,
		// Empty until the GCP client exists; auth-stack.ts is what insists on it, so the
		// data and DNS stacks can deploy first.
		googleClientId: typeof raw['google_client_id'] === 'string' ? raw['google_client_id'].trim() : '',
		authDomainPrefix: str('auth_domain_prefix'),
		allowedEmails: (emails as string[]).map((e) => e.trim().toLowerCase()).filter(Boolean),
		cognitoUserPoolId: opt(raw, 'cognito_user_pool_id'),
		cognitoClientId: opt(raw, 'cognito_client_id'),
	};
}

function opt(raw: Record<string, unknown>, key: string): string {
	const v = raw[key];
	return typeof v === 'string' ? v.trim() : '';
}

/**
 * The Cognito Hosted UI origin. Derived, never configured: Cognito builds it from the
 * domain prefix and the region, so a second copy in cloud.json could only ever be wrong.
 */
export function hostedUiDomain(cfg: CloudConfig): string {
	return `https://${cfg.authDomainPrefix}.auth.${cfg.region}.amazoncognito.com`;
}

/**
 * What must be registered on the GOOGLE OAuth client -- and it is emphatically NOT the
 * site's own `/auth/callback`, which is what this repository used to tell people to
 * register and which cost a session's debugging.
 *
 * The browser never goes from Google to this site. It goes Google -> the Cognito Hosted UI
 * -> here. Google validates `redirect_uri` against its own registered list, and the only
 * URI it is ever asked to redirect to is Cognito's `/oauth2/idpresponse`. Registering the
 * site's callback instead produces `Error 400: redirect_uri_mismatch`, in which Google
 * helpfully echoes the idpresponse URI that is missing -- a URI that appeared nowhere in
 * this repository.
 */
export function googleRedirectUri(cfg: CloudConfig): string {
	return hostedUiDomain(cfg) + '/oauth2/idpresponse';
}

/**
 * What must be registered on the COGNITO app client. Nobody has to do this by hand -- the
 * CDK sets it -- and it is named here only so it can never again be mistaken for the one
 * above.
 */
export function cognitoCallbackUrl(cfg: CloudConfig): string {
	return `https://${cfg.domain}/auth/callback`;
}

/** Called by the stack that bakes the pool's ids into code. */
export function requirePool(cfg: CloudConfig): void {
	if (!cfg.cognitoUserPoolId || !cfg.cognitoClientId) {
		fail(
			'cognito_user_pool_id and cognito_client_id are not set, so the read gate\n' +
				'  would be built with no pool to verify tokens against.\n' +
				'  Deploy SigenAuthPool first and copy its UserPoolId and ClientId outputs\n' +
				'  into cloud.json. They are CloudFormation tokens until that stack exists,\n' +
				'  and a Lambda@Edge cannot be given them as environment variables.',
		);
	}
	if (!/^[\w-]+_[0-9a-zA-Z]+$/.test(cfg.cognitoUserPoolId)) {
		fail(`cognito_user_pool_id ${cfg.cognitoUserPoolId} does not look like a pool id ` +
			`(e.g. us-east-1_AbCdEfGhI)`);
	}
}

/** Called by the stacks that cannot work without sign-in configured. */
export function requireAuth(cfg: CloudConfig): void {
	if (!cfg.googleClientId) {
		fail(
			'google_client_id is empty, so the site would have no way to sign anyone in.\n' +
				'  Create the OAuth client in the Google Cloud Console first (External consent\n' +
				'  screen), and register this as its ONE authorized redirect URI:\n\n' +
				'    ' + googleRedirectUri(cfg) + '\n\n' +
				'  That is Cognito\'s Hosted UI, NOT this site. Google redirects to Cognito,\n' +
				'  which then redirects here -- so registering ' + cognitoCallbackUrl(cfg) + '\n' +
				'  instead yields "Error 400: redirect_uri_mismatch" on every sign-in.\n' +
				'  Also check the consent screen\'s Publishing status: while it is "Testing",\n' +
				'  only accounts on its Test users list can sign in, whatever allowed_emails says.',
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
