// Cognito id-token verification, using Node built-ins only.
//
// Adapted from the same helper in the commonplace-notes project, where it lives under a
// 4096-byte inline-ZipFile limit and so had to be terse. Here the function is packaged as
// an asset, so this is a normal module -- but it stays dependency-free anyway: a
// Lambda@Edge runs on every request to a gated path, and a JWT library is a supply-chain
// surface for something crypto.verify already does.
//
// Verifies, in order: the algorithm is RS256 (never "none", never a symmetric alg an
// attacker could sign with a public key), the signature is valid for the pool's key with
// that kid, the issuer is our pool, the audience is our app client, the token is an ID
// token rather than an access token, and it has not expired.
'use strict';

const crypto = require('crypto');
const https = require('https');

let keys = null;
let fetchedAt = 0;
const TTL_MS = 3600000;

function fetchJwks(uri) {
	return new Promise((resolve, reject) => {
		https.get(uri, (res) => {
			let body = '';
			res.on('data', (d) => { body += d; });
			res.on('end', () => {
				try { resolve(JSON.parse(body).keys); } catch (e) { reject(e); }
			});
		}).on('error', reject);
	});
}

async function keyForKid(uri, kid) {
	const now = Date.now();
	if (!keys || now - fetchedAt > TTL_MS) {
		keys = await fetchJwks(uri);
		fetchedAt = now;
	}
	let jwk = keys.find((k) => k.kid === kid);
	if (!jwk) {
		// A kid we have not seen: Cognito rotates signing keys, so refetch once before
		// concluding the token is forged.
		keys = await fetchJwks(uri);
		fetchedAt = Date.now();
		jwk = keys.find((k) => k.kid === kid);
	}
	return jwk ? crypto.createPublicKey({ key: jwk, format: 'jwk' }) : null;
}

function b64urlJson(seg) {
	return JSON.parse(Buffer.from(seg, 'base64url').toString('utf8'));
}

/** The token's claims, or null. Null for every failure -- a caller must not be able to
 *  tell "expired" from "forged" and act differently on it. */
async function verifyIdToken(token, cfg) {
	try {
		const parts = token.split('.');
		if (parts.length !== 3) return null;
		const header = b64urlJson(parts[0]);
		if (header.alg !== 'RS256') return null;
		const key = await keyForKid(cfg.jwksUri, header.kid);
		if (!key) return null;
		const ok = crypto.verify(
			'RSA-SHA256',
			Buffer.from(parts[0] + '.' + parts[1]),
			key,
			Buffer.from(parts[2], 'base64url'),
		);
		if (!ok) return null;
		const claims = b64urlJson(parts[1]);
		if (claims.iss !== cfg.issuer) return null;
		if (claims.aud !== cfg.clientId) return null;
		if (claims.token_use !== 'id') return null;
		if (!claims.exp || claims.exp * 1000 <= Date.now()) return null;
		return claims;
	} catch (e) {
		return null;
	}
}

function readCookie(cookieHeader, name) {
	if (!cookieHeader) return null;
	for (const pair of cookieHeader.split(';')) {
		const i = pair.indexOf('=');
		if (i > -1 && pair.slice(0, i).trim() === name) return pair.slice(i + 1).trim();
	}
	return null;
}

module.exports = { verifyIdToken, readCookie };
