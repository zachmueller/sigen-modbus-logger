// The auth endpoints: the OAuth callback that starts a session, and the refresh that keeps
// it alive for a month without going back to Google.
//
// A REGIONAL Lambda behind the site's own CloudFront distribution at /auth/*, not a
// Lambda@Edge. Two reasons: it needs environment variables (Lambda@Edge forbids them) and
// it holds the app client secret, which has no business being replicated to every edge
// location. Being served from the site's own domain is what makes the cookies first-party,
// so they ride along to /view and /agg/* afterwards.
//
// **Why there is a refresh route at all.** This function used to keep only the id token, and
// Cognito issues one good for an hour by default -- so the gate refused it every hour and sent
// the browser back through Google, which meant Google was in practice this site's session
// store. Several sign-ins a day, each at the mercy of an account chooser. The refresh token
// was in the token-exchange response the whole time and was being thrown away. Now it is the
// session, and Google is touched about once a month. See docs/FINDINGS.md 33.
//
// Two routes, one function, because /auth/refresh needs exactly what /auth/callback needs --
// the client secret, the token endpoint, the cookie names -- and spends its grant through the
// same POST. tokenExchange() and safePath() are shared verbatim.
//
// env: COGNITO_DOMAIN (full https Hosted UI origin), CLIENT_ID, CLIENT_SECRET,
//      REDIRECT_URI (the exact URI registered on the app client), COOKIE_NAME,
//      REFRESH_COOKIE_NAME, SESSION_COOKIE_NAME, COOKIE_MAX_AGE.
'use strict';

const https = require('https');

// Defaults match COOKIES in cloud/infrastructure/lib/auth-stack.ts, which is where the names
// are decided and which sets all three as environment variables. The fallbacks exist so a
// direct `aws lambda invoke` while debugging behaves like the deployed function rather than
// setting a cookie called "undefined".
const ID_COOKIE = process.env.COOKIE_NAME || 'sigen_id';
const REFRESH_COOKIE = process.env.REFRESH_COOKIE_NAME || 'sigen_rt';
const SESSION_COOKIE = process.env.SESSION_COOKIE_NAME || 'sigen_sess';
// Thirty days. The app client's refreshTokenValidity is set to the same span, and the cookie
// must not outlive it: a browser holding a cookie whose token Cognito has already retired
// would take the /auth/refresh hop only to be refused, costing a redirect to learn nothing.
const MAX_AGE = process.env.COOKIE_MAX_AGE || String(30 * 24 * 3600);

function tokenExchange(form) {
	return new Promise((resolve, reject) => {
		const data = new URLSearchParams(form).toString();
		const url = new URL(process.env.COGNITO_DOMAIN + '/oauth2/token');
		// Client secret in the Authorization header rather than the body: it is the form
		// Cognito documents for a confidential client, and it keeps the secret out of any
		// body that might be logged.
		const auth = Buffer.from(
			process.env.CLIENT_ID + ':' + process.env.CLIENT_SECRET).toString('base64');
		const req = https.request(url, {
			method: 'POST',
			headers: {
				'Content-Type': 'application/x-www-form-urlencoded',
				'Content-Length': Buffer.byteLength(data),
				Authorization: 'Basic ' + auth,
			},
		}, (res) => {
			let body = '';
			res.on('data', (d) => { body += d; });
			res.on('end', () => resolve({ status: res.statusCode, body: body }));
		});
		req.on('error', reject);
		req.write(data);
		req.end();
	});
}

/** Only a same-site relative path, and never one of ours. Defends against an open redirect:
 *  `state` and `next` both arrive back from the outside world, and "//evil.example" is a
 *  protocol-relative URL a browser would happily follow off-site.
 *
 *  /auth/ is excluded for a different reason -- not safety but termination. Sending the end of
 *  a sign-in, or of a refresh, back to an auth endpoint is a redirect that arrives where it
 *  started, and the browser would follow it as many times as it is offered. */
function safePath(state) {
	try {
		const p = decodeURIComponent(state || '');
		if (p.startsWith('/') && !p.startsWith('//')
			&& p !== '/auth' && !p.startsWith('/auth/')) return p;
	} catch (e) { /* fall through */ }
	return '/view';
}

/** API Gateway payload format 2.0 hands cookies over already split, as `name=value` strings,
 *  so there is no Cookie header to parse here -- which is why this shares nothing with
 *  readCookie() in cloud/lambda/auth-edge/jwt.js. That one reads a raw header, because
 *  Lambda@Edge is given one. */
function readCookie(event, name) {
	for (const pair of (event && event.cookies) || []) {
		const i = pair.indexOf('=');
		if (i > -1 && pair.slice(0, i).trim() === name) return pair.slice(i + 1).trim();
	}
	return null;
}

/**
 * One Set-Cookie line.
 *
 * HttpOnly so page scripts cannot read it; Secure so it never travels in the clear;
 * SameSite=Lax so it survives the redirect back from Google but is not sent on cross-site
 * subrequests.
 *
 * `path` is load-bearing twice. It is what the browser matches when deciding to SEND the
 * cookie -- which is how the refresh token stays off /view and /agg/* -- and it is part of a
 * cookie's identity when CLEARING one, so a Max-Age=0 at the wrong path leaves the original
 * sitting there and the next request behaves as though nothing was cleared.
 */
function cookie(name, value, path, maxAge) {
	return name + '=' + value + '; HttpOnly; Secure; SameSite=Lax'
		+ '; Path=' + path + '; Max-Age=' + maxAge;
}

/**
 * The cookies a live session is made of.
 *
 *   id       what the gate verifies. Path=/ -- every gated path needs it.
 *   refresh  the session itself. **Path=/auth/**, so this long-lived credential is sent ONLY
 *            to the endpoint that spends it, and never on the hundreds of /agg/* tile fetches
 *            a page makes. The gate cannot see it, which is deliberate and is why the third
 *            cookie exists.
 *   session  when the id token was last minted, in epoch seconds. Not a credential, and it
 *            tells the gate two things: that a refresh token exists at all, and -- from its
 *            value -- whether a refresh was just attempted. See the gate's own comment.
 *
 * `refreshToken` is optional because Cognito's refresh grant does not return a new one. On
 * that path the existing cookie must be left exactly as it is: re-setting it with a fresh
 * Max-Age would silently extend a session Cognito is going to retire on schedule anyway, and
 * clearing it would end one that is still perfectly good.
 */
function sessionCookies(idToken, refreshToken) {
	const out = [
		cookie(ID_COOKIE, idToken, '/', MAX_AGE),
		cookie(SESSION_COOKIE, String(Math.floor(Date.now() / 1000)), '/', MAX_AGE),
	];
	if (refreshToken) out.push(cookie(REFRESH_COOKIE, refreshToken, '/auth/', MAX_AGE));
	return out;
}

/** Max-Age=0 at the same path each was set with. */
function clearedCookies() {
	return [
		cookie(ID_COOKIE, '', '/', 0),
		cookie(SESSION_COOKIE, '', '/', 0),
		cookie(REFRESH_COOKIE, '', '/auth/', 0),
	];
}

function redirect(dest, cookies) {
	return {
		statusCode: 302,
		headers: { Location: dest, 'Cache-Control': 'no-store' },
		cookies: cookies,
		body: '',
	};
}

function fail(message) {
	return {
		statusCode: 502,
		headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' },
		body: '<!DOCTYPE html><meta charset="utf-8"><title>Sign-in failed</title>'
			+ '<style>body{font:15px/1.5 system-ui,sans-serif;margin:12vh auto;'
			+ 'max-width:34em;padding:0 1.5rem}</style>'
			+ '<h1>Sign-in failed</h1><p>' + message + '</p>'
			+ '<p><a href="/view">Try again</a></p>',
	};
}

// One line per decision, and never the tokens: they are bearer credentials and a log is a
// place they would sit for a month. "Did the refresh work?" was the question this whole path
// exists to answer, and it must be answerable without reading a browser's cookie jar.
function log(decision, detail) {
	console.log(JSON.stringify({ auth: decision, detail: detail || undefined }));
}

/**
 * The OAuth `error` code from a refusal, and nothing else out of the body.
 *
 * WHICH code it is matters more than the status, and the status cannot tell them apart -- both
 * of these are a bare 400:
 *
 *   invalid_grant     this session has legitimately ended. Nothing is wrong; sign in again.
 *   invalid_client    THIS FUNCTION'S credential is broken, so every refresh for everyone
 *                     will fail identically, forever, until someone fixes the app client.
 *
 * One is routine and one is an outage, and without this they are the same log line. The repo
 * has already paid for that confusion once from the other direction -- FINDINGS 32, a secret
 * rotated at Google but never at Cognito, where every existing session kept working and only
 * fresh sign-ins broke.
 *
 * Guarded to the RFC 6749 shape rather than logged verbatim: a fixed vocabulary of lowercase
 * words means nothing from a response body can decide what lands in CloudWatch.
 */
function oauthError(body) {
	try {
		const code = String(JSON.parse(body).error || '');
		return /^[a-z_]{1,40}$/.test(code) ? code : 'unrecognised';
	} catch (e) {
		return 'not-json';
	}
}

// ------------------------------------------------------------------ /auth/callback

async function signIn(event) {
	const q = (event && event.queryStringParameters) || {};
	const dest = safePath(q.state);

	// Google or Cognito reported a problem rather than handing over a code -- most often
	// the user declining consent. Not an error page: say so and offer the way back.
	if (q.error) return fail('The identity provider returned: ' + String(q.error)
		.replace(/[<>&]/g, ''));
	if (!q.code) return redirect(dest, undefined);

	try {
		const res = await tokenExchange({
			grant_type: 'authorization_code',
			client_id: process.env.CLIENT_ID,
			code: q.code,
			redirect_uri: process.env.REDIRECT_URI,
		});
		if (res.status !== 200) {
			log('exchange-refused', res.status + ' ' + oauthError(res.body));
			return fail('The token exchange was rejected.');
		}
		const tokens = JSON.parse(res.body);
		if (!tokens.id_token) return fail('No id_token came back from the token exchange.');
		// The refresh token is the session. Its absence is not fatal -- the id token still
		// works for a day -- but it means every day costs a Google round trip, which is the
		// bug this route was fixed for, so it is worth a line in the log rather than silence.
		if (!tokens.refresh_token) log('no-refresh-token', 'sessions will last one id token');
		else log('signed-in');
		return redirect(dest, sessionCookies(tokens.id_token, tokens.refresh_token));
	} catch (e) {
		return fail('Could not reach the token endpoint.');
	}
}

// ------------------------------------------------------------------- /auth/refresh
//
// Reached only by a redirect from the read gate, which sends a browser here instead of to
// Google when the id token has expired but the session marker says a refresh token exists.
// Every outcome is a 302 back to where the person was going -- there is no page here, because
// there is nothing for anyone to read or do.
//
// Three failure shapes, kept apart because they want opposite handling, and getting that
// wrong is either a redirect loop or a session thrown away for nothing:
//
//   no refresh cookie    Nothing to spend. Clear the marker so the gate stops sending people
//                        here and goes to Google.
//   4xx from Cognito     The token is revoked or its month is up. Final: clear everything.
//   5xx, or unreachable  Transient. The refresh token may still be perfectly good, so it is
//                        KEPT -- but the marker is re-stamped to now, which is what makes the
//                        gate try Google on the next hop instead of sending the browser
//                        straight back here forever.

async function doRefresh(event) {
	const q = (event && event.queryStringParameters) || {};
	const dest = safePath(q.next);
	const token = readCookie(event, REFRESH_COOKIE);
	if (!token) {
		// The cookie is Path=/auth/, so this really is absence rather than a scoping mistake:
		// this request IS under /auth/.
		log('no-refresh-cookie');
		return redirect(dest, clearedCookies());
	}
	let res;
	try {
		res = await tokenExchange({
			grant_type: 'refresh_token',
			client_id: process.env.CLIENT_ID,
			refresh_token: token,
		});
	} catch (e) {
		log('refresh-unreachable', e.message);
		return redirect(dest, [restamp()]);
	}
	if (res.status >= 500) {
		log('refresh-failed', String(res.status));
		return redirect(dest, [restamp()]);
	}
	if (res.status !== 200) {
		// `invalid_grant` here is the ordinary end of a month-old session. It is also what a
		// federated user gets if Google has retired the linked account's own refresh token,
		// which happens after 7 days while the OAuth consent screen is in Testing -- see
		// cloud/README.md. Either way the answer is the same: sign in again.
		log('refresh-refused', res.status + ' ' + oauthError(res.body));
		return redirect(dest, clearedCookies());
	}
	let idToken = null;
	try { idToken = JSON.parse(res.body).id_token; } catch (e) { /* handled below */ }
	if (!idToken) {
		log('refresh-no-id-token');
		return redirect(dest, clearedCookies());
	}
	log('refreshed');
	// No refresh token argument: Cognito did not send a new one, and the existing cookie is
	// still the right one. See sessionCookies().
	return redirect(dest, sessionCookies(idToken));
}

/** The marker, moved to now, with nothing else touched. Says "a refresh was just attempted
 *  from here", which is the only thing that stops the gate from attempting another. */
function restamp() {
	return cookie(SESSION_COOKIE, String(Math.floor(Date.now() / 1000)), '/', MAX_AGE);
}

exports.handler = async (event) => {
	// rawPath, not the route key: API Gateway routes only the two paths below to this
	// function, and anything else reaching it means the routing is wrong -- in which case
	// treating it as a sign-in is the harmless reading.
	const path = (event && event.rawPath) || '';
	return path.endsWith('/auth/refresh') ? doRefresh(event) : signIn(event);
};
