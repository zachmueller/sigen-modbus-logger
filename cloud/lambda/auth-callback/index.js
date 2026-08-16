// The OAuth callback: exchanges Google's authorization code for a Cognito ID token and
// sets it as the session cookie.
//
// A REGIONAL Lambda behind the site's own CloudFront distribution at /auth/callback, not a
// Lambda@Edge. Two reasons: it needs environment variables (Lambda@Edge forbids them) and
// it holds the app client secret, which has no business being replicated to every edge
// location. Being served from the site's own domain is what makes the cookie first-party,
// so it rides along to /view and /agg/* afterwards.
//
// env: COGNITO_DOMAIN (full https Hosted UI origin), CLIENT_ID, CLIENT_SECRET,
//      REDIRECT_URI (the exact URI registered on the app client), COOKIE_NAME,
//      COOKIE_MAX_AGE.
'use strict';

const https = require('https');

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

/** Only a same-site relative path. Defends against an open redirect: `state` arrives back
 *  from the outside world, and "//evil.example" is a protocol-relative URL a browser would
 *  happily follow off-site. */
function safePath(state) {
	try {
		const p = decodeURIComponent(state || '');
		if (p.startsWith('/') && !p.startsWith('//')) return p;
	} catch (e) { /* fall through */ }
	return '/view';
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

exports.handler = async (event) => {
	const q = (event && event.queryStringParameters) || {};
	const dest = safePath(q.state);

	// Google or Cognito reported a problem rather than handing over a code -- most often
	// the user declining consent. Not an error page: say so and offer the way back.
	if (q.error) return fail('The identity provider returned: ' + String(q.error)
		.replace(/[<>&]/g, ''));
	if (!q.code) return { statusCode: 302, headers: { Location: dest }, body: '' };

	try {
		const res = await tokenExchange({
			grant_type: 'authorization_code',
			client_id: process.env.CLIENT_ID,
			code: q.code,
			redirect_uri: process.env.REDIRECT_URI,
		});
		if (res.status !== 200) return fail('The token exchange was rejected.');
		const idToken = JSON.parse(res.body).id_token;
		if (!idToken) return fail('No id_token came back from the token exchange.');
		return {
			statusCode: 302,
			headers: { Location: dest, 'Cache-Control': 'no-store' },
			// HttpOnly so page scripts cannot read it; Secure so it never travels in the
			// clear; SameSite=Lax so it survives the redirect back from Google but is not
			// sent on cross-site subrequests.
			cookies: [process.env.COOKIE_NAME + '=' + idToken
				+ '; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age='
				+ (process.env.COOKIE_MAX_AGE || '3600')],
			body: '',
		};
	} catch (e) {
		return fail('Could not reach the token endpoint.');
	}
};
