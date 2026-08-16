// Viewer-request Lambda@Edge: the read gate.
//
// Attached only to the behaviours that serve TELEMETRY -- /view, /agg/* and /api/share.
// The page code (app.js, tiles.js, charts.js, style.css) is deliberately NOT gated: it is
// published on GitHub, so gating it would protect nothing while breaking the public share
// pages, which need it to render.
//
// Two checks, and both matter:
//
//   1. The session cookie is a valid Cognito ID token for our pool and app client.
//   2. Its verified `email` claim is on the allowlist.
//
// The second is not belt-and-braces. Cognito with Google as an identity provider will
// happily authenticate ANY Google account -- there is no such thing as a Cognito pool that
// only Google users you like can sign into. So the allowlist is what actually gates the
// site, and an empty one means every visitor signs in successfully and is then refused.
// cloud/infrastructure/lib/config.ts refuses to synth with an empty list for that reason.
//
// A signed-in but not-allowlisted visitor gets 403 with a page that says who to ask -- not
// a redirect to sign in again, which would loop them through Google forever without ever
// explaining why.
//
// Lambda@Edge forbids environment variables, so config arrives as a generated module the
// stack writes at synth time. That is also why this file has no secrets in it: it needs
// only public identifiers (pool id, client id, region) plus the allowlist.
'use strict';

const cfg = require('./config');
const { verifyIdToken, readCookie } = require('./jwt');

const COOKIE = 'sigen_id';

// Paths that must answer before anyone is signed in. /auth/* is the OAuth callback -- it
// is what SETS the cookie, so gating it would make signing in impossible.
function isPublic(uri) {
	return uri.indexOf('/auth/') === 0;
}

function loginRedirect(request) {
	const host = request.headers.host[0].value;
	// Where to come back to, carried through Google and the callback. Host-relative, and
	// the callback validates it is a same-site path before honouring it.
	const state = encodeURIComponent(
		request.uri + (request.querystring ? '?' + request.querystring : ''));
	const location = cfg.hostedUiDomain + '/oauth2/authorize'
		+ '?client_id=' + encodeURIComponent(cfg.clientId)
		+ '&response_type=code'
		+ '&scope=' + encodeURIComponent('openid email profile')
		+ '&identity_provider=Google'
		+ '&redirect_uri=' + encodeURIComponent('https://' + host + '/auth/callback')
		+ '&state=' + state;
	return {
		status: '302',
		statusDescription: 'Found',
		headers: {
			location: [{ key: 'Location', value: location }],
			'cache-control': [{ key: 'Cache-Control', value: 'no-store' }],
		},
	};
}

function forbidden(email) {
	const who = String(email || 'your account').replace(/[<>&]/g, '');
	return {
		status: '403',
		statusDescription: 'Forbidden',
		headers: {
			'content-type': [{ key: 'Content-Type', value: 'text/html; charset=utf-8' }],
			'cache-control': [{ key: 'Cache-Control', value: 'no-store' }],
		},
		body: '<!DOCTYPE html><meta charset="utf-8">'
			+ '<title>Not on the list</title>'
			+ '<style>body{font:15px/1.5 system-ui,sans-serif;margin:12vh auto;max-width:34em;'
			+ 'padding:0 1.5rem;color:#222}code{background:#f2f2f2;padding:.1em .3em}</style>'
			+ '<h1>Not on the list</h1>'
			+ '<p>You signed in as <code>' + who + '</code>, which is not one of the '
			+ 'accounts this viewer is shared with.</p>'
			+ '<p>Ask the owner to add you. Signing in again with the same account will '
			+ 'not change this.</p>',
	};
}

exports.handler = async (event) => {
	const request = event.Records[0].cf.request;
	if (isPublic(request.uri)) return request;

	const cookieHeader = request.headers.cookie
		? request.headers.cookie.map((h) => h.value).join('; ')
		: '';
	const token = readCookie(cookieHeader, COOKIE);
	if (!token) return loginRedirect(request);

	const claims = await verifyIdToken(token, cfg);
	// An invalid or expired token is indistinguishable from no token, on purpose: send
	// them to sign in rather than telling them which it was.
	if (!claims) return loginRedirect(request);

	const email = String(claims.email || '').toLowerCase();
	// email_verified matters: without it, a Google account could in principle assert an
	// address it does not control, and the allowlist is keyed on the address.
	if (!email || claims.email_verified === false) return forbidden(claims.email);
	if (!cfg.allowedEmails.includes(email)) return forbidden(claims.email);
	return request;
};
