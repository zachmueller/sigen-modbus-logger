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
				COOKIE_NAME: 'sigen_id',
				// Twelve hours: long enough not to interrupt an afternoon of looking at
				// charts, short enough that a laptop left somewhere stops working today.
				// The id token inside expires in an hour, so the edge function sends them
				// back through Google after that -- silent while their Google session holds.
				COOKIE_MAX_AGE: '43200',
			},
			logGroup: new cdk.aws_logs.LogGroup(this, 'CallbackLogs', {
				retention: cdk.aws_logs.RetentionDays.ONE_MONTH,
				removalPolicy: cdk.RemovalPolicy.DESTROY,
			}),
			description: 'OAuth code -> id token -> first-party session cookie.',
		});

		const api = new cdk.aws_apigatewayv2.CfnApi(this, 'AuthApi', {
			name: `sigen-auth-${this.stackName}`, protocolType: 'HTTP',
		});
		const integration = new cdk.aws_apigatewayv2.CfnIntegration(this, 'AuthIntegration', {
			apiId: api.ref, integrationType: 'AWS_PROXY',
			integrationUri: callbackFn.functionArn,
			integrationMethod: 'POST', payloadFormatVersion: '2.0',
		});
		new cdk.aws_apigatewayv2.CfnRoute(this, 'AuthRoute', {
			apiId: api.ref, routeKey: 'GET /auth/callback',
			target: cdk.Fn.join('', ['integrations/', integration.ref]),
		});
		new cdk.aws_apigatewayv2.CfnStage(this, 'AuthStage', {
			apiId: api.ref, stageName: '$default', autoDeploy: true,
		});
		callbackFn.addPermission('ApiInvoke', {
			principal: new cdk.aws_iam.ServicePrincipal('apigateway.amazonaws.com'),
			sourceArn: `arn:aws:execute-api:${this.region}:${this.account}:${api.ref}/*/*/auth/callback`,
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
