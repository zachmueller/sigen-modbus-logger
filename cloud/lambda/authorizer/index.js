// The live API's Lambda authorizer: the edge gate's check, for API Gateway.
//
// HTTP API, payload format 2.0, simple responses. Attached to every /api/tracker/* route from
// the start, and to the live routes once cloud.json's live_public is false.
//
// WHY A SECOND CHECK when CloudFront's edge gate already runs on those paths: the API has its own
// hostname, `<id>.execute-api.<region>.amazonaws.com`, reachable beside CloudFront by anyone who
// learns it. The edge gate cannot protect a request that never passes through CloudFront, so the
// API checks too -- the same token, the same issuer, the same allowlist, from the same generated
// config.js (auth-stack.ts gateConfig()), with the same verifier (auth-edge/jwt.js, copied in at
// synth). Two copies of one decision, made by one piece of code.
//
// The cookie name comes from config.js, never a literal: auth-stack.ts's COOKIES is the one list
// of names, and tests/test_web.py refuses the literal in the gate's code.
'use strict';

const { createHash } = require('crypto');
const cfg = require('./config');
const { verifyIdToken, readCookie } = require('./jwt');

function deny(why, detail) {
	// One JSON line per refusal, never the token or the address -- the edge gate's logging rule.
	console.log(JSON.stringify({ decision: 'deny', why, detail: detail || undefined }));
	return { isAuthorized: false };
}

exports.handler = async (event) => {
	// Payload 2.0 hands cookies over as an array; the header form is a fallback for a client that
	// sends them some other way.
	const header = (event.cookies && event.cookies.length)
		? event.cookies.join('; ')
		: ((event.headers && event.headers.cookie) || '');
	const token = readCookie(header, cfg.cookies.id);
	if (!token) return deny('no-cookie');

	const { claims, reason } = await verifyIdToken(token, cfg);
	if (!claims) return deny('verify-failed', reason);

	const email = String(claims.email || '').toLowerCase();
	if (!email || claims.email_verified === false) return deny('email-unverified');
	if (!cfg.allowedEmails.includes(email)) return deny('not-allowlisted');

	// A short hash, not the address: it is what an event records as `by`, so the table holds
	// "the same person did these" without holding who.
	return {
		isAuthorized: true,
		context: { email_hash: createHash('sha256').update(email).digest('hex').slice(0, 12) },
	};
};
