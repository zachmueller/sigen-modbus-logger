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
// It has one job beyond the gate, and only on /api/share: signing the POST body's hash into
// `x-amz-content-sha256`, which CloudFront's OAC requires and Lambda function URLs refuse to
// do without. See signPayload() -- the reasoning is long and belongs next to the code.
//
// Lambda@Edge forbids environment variables, so config arrives as a generated module the
// stack writes at synth time. That is also why this file has no secrets in it: it needs
// only public identifiers (pool id, client id, region) plus the allowlist.
'use strict';

const { createHash } = require('crypto');

const cfg = require('./config');
const { verifyIdToken, readCookie } = require('./jwt');

const COOKIE = 'sigen_id';

// Paths that must answer before anyone is signed in. /auth/* is the OAuth callback -- it
// is what SETS the cookie, so gating it would make signing in impossible.
function isPublic(uri) {
	return uri.indexOf('/auth/') === 0;
}

// Every refusal an API caller can get. `ok: false` and `error` are the shape app.js reads,
// and it reads them the same way whoever refused -- the gate here, or the share handler
// behind it. A refusal that does not carry `error` can only reach the page as a bare status
// code, which is exactly how the OAC signature bug below stayed invisible.
function refusalJson(status, statusDescription, obj) {
	return {
		status: String(status),
		statusDescription: statusDescription,
		headers: {
			'content-type': [{ key: 'Content-Type', value: 'application/json' }],
			'cache-control': [{ key: 'Cache-Control', value: 'no-store' }],
		},
		body: JSON.stringify(obj),
	};
}

// An API call cannot follow a redirect usefully. fetch() would chase the 302 to Google, get
// a sign-in page back, and hand app.js an HTML body where it expected JSON -- so the page
// would report a parse error for what is really an expired session. A status it can branch on
// instead, with `login` naming where to send the person.
function unauthorizedJson(request, reason) {
	const host = request.headers.host[0].value;
	return refusalJson(401, 'Unauthorized', {
		ok: false,
		error: reason,
		login: 'https://' + host + '/view',
	});
}

/** Refused for good: no `login`, because signing in again cannot change the answer. */
function forbiddenJson(reason) {
	return refusalJson(403, 'Forbidden', { ok: false, error: reason });
}

/** Paths whose caller is a script, not a browser navigating. */
function isApi(uri) {
	return uri.indexOf('/api/') === 0;
}

// ---------------------------------------------------------------- signing the payload
//
// /api/share is a Lambda function URL with AWS_IAM auth, reached through CloudFront Origin
// Access Control. OAC signs every origin request with SigV4 -- and per the CloudFront
// documentation for a Lambda function URL origin: *if you use PUT or POST, the payload hash
// of the request body must arrive in the `x-amz-content-sha256` header, because Lambda does
// not support unsigned payloads.* Without it Lambda recomputes the body hash, the signature
// does not match, and the function URL answers **403 `{"Message":"Forbidden"}`**.
//
// That 403 is refused at Lambda's authorizer, so the handler is never invoked and its log
// group stays EMPTY. The only trace anywhere was this function's own `{"gate":"allow"}`
// line, and the page -- reading `error` from a body that has only `Message` -- could say no
// more than "403 ". Every browser click of "Create link" failed this way; the endpoint had
// only ever been tested by direct invocation, which does not pass through CloudFront.
//
// It is signed HERE rather than in web/app.js because the OAC is what demands it: one place,
// and any caller of /api/share works. See docs/FINDINGS.md 27.
const EMPTY_SHA256 = createHash('sha256').update('').digest('hex');

/**
 * Sets `x-amz-content-sha256` over the body. Returns the number of bytes hashed, or null if
 * the body was too big to hash -- which is NOT a case to guess at:
 *
 *   CloudFront truncates a viewer-request body at 40 KB before exposing it here, but sends
 *   the FULL original body to the origin whenever the function leaves it read-only, as this
 *   one does. So hashing a truncated body signs bytes the origin will not receive, and the
 *   result is the same 403 as signing nothing -- one layer further down, with the header
 *   present and looking correct. The caller refuses instead.
 *
 * A body-less request hashes to the empty digest, which is what CloudFront would sign for it
 * anyway, so GET and POST take the same path.
 */
function signPayload(request) {
	const body = request.body;
	if (body && body.inputTruncated) return null;
	const raw = body && body.data
		? Buffer.from(body.data, body.encoding === 'text' ? 'utf8' : 'base64')
		: null;
	request.headers['x-amz-content-sha256'] = [{
		key: 'x-amz-content-sha256',
		value: raw ? createHash('sha256').update(raw).digest('hex') : EMPTY_SHA256,
	}];
	return raw ? raw.length : 0;
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

// One line per decision, because this function used to emit NOTHING -- and a sign-in that
// silently bounced could not be told from one that silently worked, so diagnosing it meant
// reading a browser's cookie jar instead of a log.
//
// Two things are deliberately absent. The token: it is a bearer credential, and a log is a
// place it would sit for a month. The full address: it is someone's identity, and the
// domain is enough to tell which of a handful of allowlisted people hit a gate.
//
// Allows are logged only for page requests, not for every /agg/* tile the page then
// fetches -- same reasoning as serve.py's `noisy` filter. "Did this person get in?" is
// answered by one line; a line per tile would bury it.
function log(decision, request, detail) {
	console.log(JSON.stringify({
		gate: decision,
		uri: request.uri,
		detail: detail || undefined,
	}));
}

function isTelemetryFetch(uri) {
	return uri.indexOf('/agg/') === 0 || uri.indexOf('/share/') === 0;
}

/** The domain only. Enough to diagnose, not a record of who was here. */
function domainOf(email) {
	const at = String(email || '').indexOf('@');
	return at < 0 ? 'no-domain' : String(email).slice(at).toLowerCase();
}

exports.handler = async (event) => {
	const request = event.Records[0].cf.request;
	if (isPublic(request.uri)) return request;

	const cookieHeader = request.headers.cookie
		? request.headers.cookie.map((h) => h.value).join('; ')
		: '';
	const token = readCookie(cookieHeader, COOKIE);
	if (!token) {
		log('no-cookie', request);
		return isApi(request.uri)
			? unauthorizedJson(request, 'not signed in')
			: loginRedirect(request);
	}

	const { claims, reason } = await verifyIdToken(token, cfg);
	// An invalid or expired token is indistinguishable from no token TO THE VISITOR, on
	// purpose: send them to sign in rather than telling them which it was. `reason` is for
	// the log only, and does not change the response.
	if (!claims) {
		log('verify-failed', request, reason);
		return isApi(request.uri)
			? unauthorizedJson(request, 'your session has expired')
			: loginRedirect(request);
	}

	const email = String(claims.email || '').toLowerCase();
	// email_verified matters: without it, a Google account could in principle assert an
	// address it does not control, and the allowlist is keyed on the address.
	if (!email || claims.email_verified === false) {
		log('email-unverified', request, domainOf(claims.email));
		return isApi(request.uri)
			? forbiddenJson('your Google account has no verified email address')
			: forbidden(claims.email);
	}
	if (!cfg.allowedEmails.includes(email)) {
		log('not-allowlisted', request, domainOf(email));
		// 403, not 401: signing in again cannot help, so the page must not offer to.
		return isApi(request.uri)
			? forbiddenJson('this account is not on the viewer\'s list')
			: forbidden(claims.email);
	}
	// Signed only for the API paths, which are the only ones behind a function URL, and only
	// after the allowlist -- an anonymous caller must never get this far. See signPayload().
	//
	// The length, never the body: a share note is someone's prose. But it IS logged, because
	// "did the payload get signed?" was unanswerable from any log while this was broken --
	// including the case below, where the behaviour was never given a body to sign and a POST
	// is about to be refused by something that logs nothing at all.
	let detail;
	if (isApi(request.uri)) {
		if (!request.body) {
			detail = 'no body exposed; a POST will be refused unsigned';
		} else {
			const bytes = signPayload(request);
			if (bytes === null) {
				log('body-too-large', request);
				return refusalJson(413, 'Payload Too Large', {
					ok: false,
					error: 'that request body is over 40 KB, which is more than this endpoint '
						+ 'can sign -- shorten the note or the field list',
				});
			}
			detail = bytes + ' body bytes signed';
		}
	}
	if (!isTelemetryFetch(request.uri)) log('allow', request, detail);
	return request;
};
