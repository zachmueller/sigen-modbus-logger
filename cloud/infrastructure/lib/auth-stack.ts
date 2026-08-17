import * as cdk from 'aws-cdk-lib';
import * as fs from 'fs';
import * as path from 'path';
import { Construct } from 'constructs';
import {
	CloudConfig, requireAuth, requirePool,
	hostedUiDomain, googleRedirectUri, cognitoCallbackUrl,
} from './config';

const LAMBDA_DIR = path.resolve(__dirname, '../../lambda');

/**
 * The three cookies a session is made of, named ONCE for the two things that must agree.
 *
 * The callback Lambda SETS them and the edge gate READS them, and they are separate
 * deployment artefacts -- one takes environment variables, the other has them baked into a
 * generated module. So a name typed twice is a name that can drift, and the symptom of the
 * drift is a site that signs you in and then behaves as though you had never arrived.
 *
 *   id       the Cognito id token. What the gate verifies; short-lived by Cognito's rules.
 *   refresh  the Cognito refresh token, and the actual session. Path=/auth/ -- see below.
 *   session  epoch seconds when the id token was last minted. NOT a credential.
 *
 * `session` exists because the gate cannot see `refresh`, deliberately: the refresh token is
 * scoped to Path=/auth/ so it never rides along on /view or the /agg/* telemetry fetches that
 * make up almost every request. The gate therefore needs some other way to know a refresh is
 * worth attempting, and its value doubles as the loop-breaker -- a refresh that just happened
 * and did not produce a usable token must not be attempted again. Both clocks in that
 * comparison are AWS-side.
 */
const COOKIES = {
	id: 'sigen_id',
	refresh: 'sigen_rt',
	session: 'sigen_sess',
};

/**
 * Thirty days, and what that gave up.
 *
 * This was twelve hours, with the reasoning "long enough not to interrupt an afternoon of
 * looking at charts, short enough that a laptop left somewhere stops working today". But the
 * id token inside expired in an HOUR -- Cognito's default -- and nothing here kept the refresh
 * token, so the gate sent people back through Google every hour and Google was in practice
 * this site's session store. Several re-authentications a day, each one at the mercy of
 * Google's account chooser.
 *
 * What makes a month defensible is not the cookie: it is that the ALLOWLIST IS RE-CHECKED AT
 * THE EDGE ON EVERY REQUEST. Removing an address takes effect on the next tile fetch no matter
 * what any token's lifetime says, so the long-lived credential does not extend anyone's
 * access -- it only saves them a redirect. What is genuinely given up is the stolen-laptop
 * case, which is now revoked by deleting the user from the pool or rotating the app client
 * rather than by waiting until this evening.
 */
const SESSION_MAX_AGE_S = 30 * 24 * 3600;

/**
 * Google sign-in, in two stacks, and the reason it is two.
 *
 * A viewer-request Lambda@Edge cannot have environment variables -- AWS forbids them --
 * so the pool id and app client id have to be IN its code. But asset bundling happens at
 * synth, while those ids are CloudFormation tokens that do not exist until deploy. Writing
 * them into a generated module at synth writes the literal string "${Token[...]}", and the
 * gate then rejects every valid token with no clue why.
 *
 * commonplace-notes solves this by building the whole function body with Fn::Sub into an
 * inline ZipFile, which CloudFormation resolves at deploy -- at the cost of a 4096-byte
 * limit on the code. This function plus its JWT verifier is about 7 KB, so that door is
 * shut.
 *
 * So: PoolStack creates Cognito and prints its ids; you put them in cloud.json; EdgeStack
 * bakes them into the function. Same shape as the ACM certificate, which is also created
 * first and referenced by identifier afterwards, and for the same reason -- some facts are
 * only knowable after something else exists.
 *
 *     cdk deploy SigenAuthPool          # prints UserPoolId and ClientId
 *     # put both in cloud.json
 *     cdk deploy SigenAuthEdge          # bakes them in, prints the edge ARN
 *
 * Both are us-east-1, because Lambda@Edge can only be published there.
 */

export interface PoolStackProps extends cdk.StackProps {
	cfg: CloudConfig;
}

/** Cognito, Google, and the callback that turns a code into a first-party cookie. */
export class AuthPoolStack extends cdk.Stack {
	public readonly callbackApiDomain: string;

	constructor(scope: Construct, id: string, props: PoolStackProps) {
		super(scope, id, props);
		const { cfg } = props;
		requireAuth(cfg);

		const clientSecret = new cdk.CfnParameter(this, 'GoogleClientSecret', {
			type: 'String',
			noEcho: true,
			minLength: 1,
			description: 'Google OAuth client secret. Not stored anywhere: passed at deploy.',
		});

		const pool = new cdk.aws_cognito.UserPool(this, 'Pool', {
			userPoolName: `sigen-${this.stackName}`,
			signInAliases: { email: true },
			// Nothing ever signs in with a password: Google is the only provider, so there
			// is no password policy worth tuning and no self-signup surface. Who gets past
			// Google is decided by the allowlist in the edge function, not here -- see
			// EdgeStack.
			selfSignUpEnabled: false,
			removalPolicy: cdk.RemovalPolicy.DESTROY,
		});

		const google = new cdk.aws_cognito.UserPoolIdentityProviderGoogle(this, 'Google', {
			userPool: pool,
			clientId: cfg.googleClientId,
			clientSecretValue: cdk.SecretValue.cfnParameter(clientSecret),
			scopes: ['openid', 'email', 'profile'],
			attributeMapping: {
				email: cdk.aws_cognito.ProviderAttribute.GOOGLE_EMAIL,
				// Mapped through so the edge function can insist Google verified the
				// address before matching it against a list keyed on addresses.
				emailVerified: cdk.aws_cognito.ProviderAttribute.other('email_verified'),
			},
		});

		pool.addDomain('HostedUi', {
			cognitoDomain: { domainPrefix: cfg.authDomainPrefix },
		});

		const callbackUrl = cognitoCallbackUrl(cfg);
		const client = pool.addClient('Client', {
			generateSecret: true,
			supportedIdentityProviders: [
				cdk.aws_cognito.UserPoolClientIdentityProvider.GOOGLE,
			],
			oAuth: {
				flows: { authorizationCodeGrant: true },
				scopes: [cdk.aws_cognito.OAuthScope.OPENID,
					cdk.aws_cognito.OAuthScope.EMAIL,
					cdk.aws_cognito.OAuthScope.PROFILE],
				callbackUrls: [callbackUrl],
				logoutUrls: [`https://${cfg.domain}/`],
			},
			// ALLOW_REFRESH_TOKEN_AUTH, and nothing else -- which is what this odd-looking
			// literal spells. CDK emits ExplicitAuthFlows only when `authFlows` is a NON-EMPTY
			// object, and then always appends ALLOW_REFRESH_TOKEN_AUTH itself; `{}` is treated
			// as "unspecified" and emits nothing. So one flag set to false is how you ask for
			// exactly the refresh flow. (aws-cdk-lib 2.265.0, configureAuthFlows().)
			//
			// Left unspecified, the deployed client reported ExplicitAuthFlows: null and got
			// Cognito's legacy defaults -- which do include refresh-token auth, so this
			// probably worked already. "Probably" is not good enough for the one mechanism the
			// whole 30-day session rests on, and being explicit also narrows the client to what
			// it actually uses: nothing here ever signs in with a password or SRP, since Google
			// is the only provider and self-signup is off.
			//
			// This does NOT touch the hosted UI's code exchange, which is governed by
			// allowedOAuthFlows above -- a separate property, and a separate mechanism.
			authFlows: { userSrp: false },
			// Both explicit, because the DEFAULT is what made signing in a daily chore:
			// Cognito issues an id token good for one hour unless told otherwise, and the
			// gate has no choice but to refuse it after that.
			//
			// 24 h is Cognito's maximum for an id token. It is not the security boundary
			// here -- the allowlist is, and it is re-read on every request -- so the only
			// question it settles is how often a browser takes the silent /auth/refresh
			// hop, and the answer is once a day.
			idTokenValidity: cdk.Duration.hours(24),
			// The actual session length. Stated rather than left to the default, which
			// happens to be 30 days today: a value this change depends on should not be
			// one a future Cognito release could quietly alter.
			//
			// It does NOT slide. Cognito's refresh grant returns no new refresh token, so
			// the month runs from sign-in and a browser goes through Google once a month.
			refreshTokenValidity: cdk.Duration.seconds(SESSION_MAX_AGE_S),
		});
		client.node.addDependency(google);

		const hostedUi = hostedUiDomain(cfg);

		const callbackFn = new cdk.aws_lambda.Function(this, 'AuthCallbackFn', {
			runtime: cdk.aws_lambda.Runtime.NODEJS_22_X,
			handler: 'index.handler',
			code: cdk.aws_lambda.Code.fromAsset(path.join(LAMBDA_DIR, 'auth-callback')),
			timeout: cdk.Duration.seconds(10),
			environment: {
				COGNITO_DOMAIN: hostedUi,
				CLIENT_ID: client.userPoolClientId,
				// Cognito's own client secret, not Google's. The callback needs it to
				// exchange the code; a regional Lambda can hold it, which is one of the two
				// reasons this is not a Lambda@Edge.
				CLIENT_SECRET: client.userPoolClientSecret.unsafeUnwrap(),
				// Explicit rather than derived from the Host header, so it cannot drift
				// from the URI registered on the app client.
				REDIRECT_URI: callbackUrl,
				// All three from COOKIES above, which the edge gate is given the same way.
				// Two artefacts, one list of names.
				COOKIE_NAME: COOKIES.id,
				REFRESH_COOKIE_NAME: COOKIES.refresh,
				SESSION_COOKIE_NAME: COOKIES.session,
				// Thirty days; see SESSION_MAX_AGE_S for what that traded away. The refresh
				// token's own validity is set on the app client above, and this is the cookie
				// that carries it -- a cookie outliving the token would leave the gate
				// attempting a refresh that can only fail, which costs a redirect.
				COOKIE_MAX_AGE: String(SESSION_MAX_AGE_S),
			},
			logGroup: new cdk.aws_logs.LogGroup(this, 'CallbackLogs', {
				retention: cdk.aws_logs.RetentionDays.ONE_MONTH,
				removalPolicy: cdk.RemovalPolicy.DESTROY,
			}),
			description: 'OAuth code -> id token -> cookie, and the refresh that renews it.',
		});

		const api = new cdk.aws_apigatewayv2.CfnApi(this, 'AuthApi', {
			name: `sigen-auth-${this.stackName}`, protocolType: 'HTTP',
		});
		const integration = new cdk.aws_apigatewayv2.CfnIntegration(this, 'AuthIntegration', {
			apiId: api.ref, integrationType: 'AWS_PROXY',
			integrationUri: callbackFn.functionArn,
			integrationMethod: 'POST', payloadFormatVersion: '2.0',
		});
		// Two routes, one integration, one function. /auth/refresh needs exactly what
		// /auth/callback needs -- the client secret, the token endpoint and the cookie names --
		// and it spends a refresh token through the same POST to /oauth2/token, so a second
		// Lambda would be a second copy of all of it.
		for (const [name, routeKey] of [
			['AuthRoute', 'GET /auth/callback'],
			['AuthRefreshRoute', 'GET /auth/refresh'],
		] as const) {
			new cdk.aws_apigatewayv2.CfnRoute(this, name, {
				apiId: api.ref, routeKey: routeKey,
				target: cdk.Fn.join('', ['integrations/', integration.ref]),
			});
		}
		new cdk.aws_apigatewayv2.CfnStage(this, 'AuthStage', {
			apiId: api.ref, stageName: '$default', autoDeploy: true,
		});
		// One permission per route, named, rather than one wildcard over /auth/*. What may
		// invoke a function holding the client secret is a security boundary, and a boundary
		// is worth writing out: adding a third route should be a deliberate line here, not
		// something a wildcard grants in advance.
		callbackFn.addPermission('ApiInvoke', {
			principal: new cdk.aws_iam.ServicePrincipal('apigateway.amazonaws.com'),
			sourceArn: `arn:aws:execute-api:${this.region}:${this.account}:${api.ref}/*/*/auth/callback`,
		});
		callbackFn.addPermission('ApiInvokeRefresh', {
			principal: new cdk.aws_iam.ServicePrincipal('apigateway.amazonaws.com'),
			sourceArn: `arn:aws:execute-api:${this.region}:${this.account}:${api.ref}/*/*/auth/refresh`,
		});
		this.callbackApiDomain = `${api.ref}.execute-api.${this.region}.amazonaws.com`;

		new cdk.CfnOutput(this, 'UserPoolId', {
			value: pool.userPoolId,
			description: 'Put this in cloud.json as cognito_user_pool_id',
		});
		new cdk.CfnOutput(this, 'ClientId', {
			value: client.userPoolClientId,
			description: 'Put this in cloud.json as cognito_client_id',
		});
		new cdk.CfnOutput(this, 'CallbackApiDomain', { value: this.callbackApiDomain });
		new cdk.CfnOutput(this, 'HostedUiDomain', { value: hostedUi });
		// The two redirect URIs, named apart, because conflating them is a whole evening.
		// This one is a manual step and the only one Google ever needs.
		new cdk.CfnOutput(this, 'GoogleAuthorizedRedirectUri', {
			value: googleRedirectUri(cfg),
			description: 'MANUAL: register this, and only this, on the Google OAuth client. '
				+ 'It is Cognito\'s Hosted UI, not this site -- Google redirects to Cognito, '
				+ 'which redirects here. Registering the site callback gives '
				+ 'redirect_uri_mismatch.',
		});
		// This one needs no action; it is printed so it is never mistaken for the above.
		new cdk.CfnOutput(this, 'CognitoCallbackUrl', {
			value: callbackUrl,
			description: 'Set by this stack on the app client. NOT a Google setting.',
		});
	}
}

/** The read gate: a viewer-request Lambda@Edge with the ids and the allowlist baked in. */
export class AuthEdgeStack extends cdk.Stack {
	public readonly edgeVersion: cdk.aws_lambda.IVersion;

	constructor(scope: Construct, id: string, props: PoolStackProps) {
		super(scope, id, props);
		const { cfg } = props;
		requireAuth(cfg);
		requirePool(cfg);

		// cfg.region, not props.env.region: bin/app.ts sets the latter FROM the former, so
		// they are the same string, and deriving the gate's issuer from the same field the
		// Hosted UI domain comes from means there is one place a region can be wrong.
		const issuer =
			`https://cognito-idp.${cfg.region}.amazonaws.com/${cfg.cognitoUserPoolId}`;
		// Every value here is a literal from cloud.json, which is the whole point of the
		// two-stack split: nothing below is a CloudFormation token, so it survives being
		// written to a file at synth time.
		const config = {
			clientId: cfg.cognitoClientId,
			issuer: issuer,
			jwksUri: issuer + '/.well-known/jwks.json',
			hostedUiDomain: hostedUiDomain(cfg),
			allowedEmails: cfg.allowedEmails,
			// The same object the callback Lambda is given as environment variables, so the
			// function that READS the cookies and the function that SETS them cannot disagree
			// about what they are called. See COOKIES at the top of this file.
			cookies: COOKIES,
		};

		const code = cdk.aws_lambda.Code.fromAsset(path.join(LAMBDA_DIR, 'auth-edge'), {
			assetHashType: cdk.AssetHashType.OUTPUT,
			bundling: {
				image: cdk.DockerImage.fromRegistry('scratch'),
				local: {
					tryBundle(outputDir: string): boolean {
						for (const f of ['index.js', 'jwt.js']) {
							fs.copyFileSync(path.join(LAMBDA_DIR, 'auth-edge', f),
								path.join(outputDir, f));
						}
						// A module the handler `require`s, rather than text concatenated
						// ahead of it: easier to read, and impossible to break with a
						// quoting mistake in a template substitution.
						fs.writeFileSync(path.join(outputDir, 'config.js'),
							'// GENERATED AT SYNTH from cloud.json. Do not edit.\n'
							+ 'module.exports = ' + JSON.stringify(config, null, 2) + ';\n');
						return true;
					},
				},
			},
		});

		const fn = new cdk.aws_cloudfront.experimental.EdgeFunction(this, 'AuthEdgeFn', {
			runtime: cdk.aws_lambda.Runtime.NODEJS_22_X,
			handler: 'index.handler',
			code: code,
			// Viewer-request functions are capped at 5 s. The only slow path is a JWKS
			// fetch for a signing key this replica has not seen, cached an hour after that.
			timeout: cdk.Duration.seconds(5),
			memorySize: 128,
			description: 'Read gate: verify the Cognito id token, then check the allowlist.',
		});
		this.edgeVersion = fn.currentVersion;

		new cdk.CfnOutput(this, 'EdgeFunctionVersionArn', { value: this.edgeVersion.edgeArn });
		new cdk.CfnOutput(this, 'AllowedEmailCount', {
			value: String(cfg.allowedEmails.length),
			// The count, never the addresses. A stack output is readable by anyone with
			// CloudFormation access, and this is other people's identities.
			description: 'How many addresses the gate will admit',
		});
	}
}
