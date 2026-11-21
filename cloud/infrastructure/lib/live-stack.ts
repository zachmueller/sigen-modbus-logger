/**
 * The live stream's cloud half, in `live_region` rather than us-east-1.
 *
 * **Why a second region at all.** Every other stack is in us-east-1 because Lambda@Edge and
 * CloudFront's certificate must be (bin/app.ts). This one is about latency to a house in New
 * Zealand: a message every two seconds from the Pi, and a browser watching it arrive. AWS IoT
 * Core is not offered in ap-southeast-6 (Auckland), so it is Sydney, ap-southeast-2 -- the
 * nearest region that has every service below.
 *
 * The pieces, in the order a message meets them:
 *
 *   Pi (live.py) --MQTT/TLS, X.509--> IoT Core --topic rule--> DynamoDB "live" table
 *
 * and, added by later phases, the API that seeds a browser from that table and hands it a
 * short-lived URL to subscribe to the same topic.
 *
 * **What the Pi may do: connect as one client id, publish to one topic. Nothing else.** It
 * gets no DynamoDB permission at all -- the rule writes the table with its own role -- so a
 * certificate lifted off the Pi can publish nonsense to one topic and that is the whole of
 * it. The certificate is created by hand (cloud/README.md), so its private key never passes
 * through CloudFormation or a stack output; this stack only attaches a policy to it, by id.
 */
import * as cdk from 'aws-cdk-lib';
import * as fs from 'fs';
import * as path from 'path';
import { Construct } from 'constructs';
import { ownPackageLambdaCode } from './archive-bundle';
import { gateConfig } from './auth-stack';
import { CloudConfig } from './config';

const LAMBDA_DIR = path.resolve(__dirname, '../../lambda');

/** The one topic. live.py's live_topic default in config.py must equal it; a test checks. */
export const LIVE_TOPIC = 'sigen/live';
/** The Pi's MQTT client id. config.py's live_client_id default; a test checks. */
export const PI_CLIENT_ID = 'sigen-pi';
/** How long a message stays in the table: the width of the live chart. */
export const LIVE_WINDOW_S = 300;
/** Concurrent viewers, all told. IoT allows one connection per client id, so this is a hard cap. */
export const VIEWER_SLOTS = 10;
/** Of those, how many an anonymous visitor may take. The rest are for signed-in callers. */
export const PUBLIC_SLOTS = 8;
/** Viewer client ids are this plus the slot number. */
export const VIEWER_CLIENT_PREFIX = 'sigen-view-';

export interface LiveStackProps extends cdk.StackProps {
	cfg: CloudConfig;
}

export class LiveStack extends cdk.Stack {
	readonly table: cdk.aws_dynamodb.Table;
	/** `<id>.execute-api.<region>.amazonaws.com`, for SigenSite's /api/live/* origin. */
	readonly apiDomain: string;

	constructor(scope: Construct, id: string, props: LiveStackProps) {
		super(scope, id, props);
		const { cfg } = props;
		const arn = (resource: string) =>
			`arn:${this.partition}:iot:${this.region}:${this.account}:${resource}`;

		// ---- the five-minute cache ------------------------------------------------------
		// Its whole job is the cold start: a page opened now has nothing to draw until the
		// stream delivers, so it reads the last five minutes from here first.
		//
		// PROVISIONED, not on-demand. Twelve RCU and four WCU across both of this stack's
		// tables sit inside DynamoDB's always-free allowance, and on-demand has no free tier at
		// all; at one write every two seconds the difference is the whole bill. Two WCU is four
		// times the average write rate (0.5/s at under 1 KB), and burst capacity absorbs the
		// rest. Ten RCU because a seed is a Query over ~150 items of ~400 B, ~8 RCU eventually
		// consistent, and CloudFront caches the seed for two seconds so a crowd costs one.
		//
		// A single partition, `live`, sorted by the record's own timestamp in ms. A hot
		// partition is the textbook mistake, at thousands of writes a second; this is one write
		// every two seconds, and one partition is what makes "the last five minutes" one Query.
		//
		// TTL deletes LAZILY -- typically within a day, not at the second -- so `ttl` keeps the
		// table from growing, and the seed's own key condition is what keeps old items off the
		// page.
		//
		// DESTROY: everything here is a copy of the last five minutes of something the archive
		// holds permanently.
		this.table = new cdk.aws_dynamodb.Table(this, 'LiveTable', {
			partitionKey: { name: 'pk', type: cdk.aws_dynamodb.AttributeType.STRING },
			sortKey: { name: 't', type: cdk.aws_dynamodb.AttributeType.NUMBER },
			billingMode: cdk.aws_dynamodb.BillingMode.PROVISIONED,
			readCapacity: 10,
			writeCapacity: 2,
			timeToLiveAttribute: 'ttl',
			removalPolicy: cdk.RemovalPolicy.DESTROY,
		});

		// ---- what the Pi may do ---------------------------------------------------------
		// An IoT policy rather than IAM: the Pi authenticates with an X.509 certificate, and
		// a certificate principal is authorized by IoT policies only.
		const piPolicy = new cdk.aws_iot.CfnPolicy(this, 'PiPolicy', {
			policyDocument: {
				Version: '2012-10-17',
				Statement: [
					// One client id. IoT allows one connection per id, so a second device
					// presenting this certificate knocks the first off rather than joining it.
					{ Effect: 'Allow', Action: 'iot:Connect',
					  Resource: arn(`client/${PI_CLIENT_ID}`) },
					// One topic. No Subscribe, no Receive: the Pi has nothing to listen to.
					{ Effect: 'Allow', Action: 'iot:Publish',
					  Resource: arn(`topic/${LIVE_TOPIC}`) },
				],
			},
		});

		// Attached by id, and only once the id exists: the certificate is made by hand so its
		// private key goes straight from IoT to a gitignored file (cloud/README.md). Until
		// live_cert_id is set the stack deploys without it, and the Pi simply cannot connect.
		if (cfg.liveCertId) {
			const certArn = arn(`cert/${cfg.liveCertId}`);
			new cdk.aws_iot.CfnPolicyPrincipalAttachment(this, 'PiPolicyAttachment', {
				policyName: piPolicy.ref,
				principal: certArn,
			});
			// A thing named for the client id, so the console names the connection rather
			// than showing a bare certificate hash. Authorizes nothing by itself.
			const thing = new cdk.aws_iot.CfnThing(this, 'PiThing', { thingName: PI_CLIENT_ID });
			new cdk.aws_iot.CfnThingPrincipalAttachment(this, 'PiThingAttachment', {
				thingName: thing.ref,
				principal: certArn,
			});
		}

		// ---- topic -> table ---------------------------------------------------------------
		// The rule adds the two attributes the message does not carry: the partition key, and
		// the TTL -- from IoT's own clock, not the Pi's, so a Pi with a wrong clock cannot write
		// items that never expire.
		const ruleRole = new cdk.aws_iam.Role(this, 'LiveRuleRole', {
			assumedBy: new cdk.aws_iam.ServicePrincipal('iot.amazonaws.com'),
			description: 'Lets the live topic rule write the live partition, and nothing else',
		});
		ruleRole.addToPolicy(new cdk.aws_iam.PolicyStatement({
			actions: ['dynamodb:PutItem'],
			resources: [this.table.tableArn],
			// Only the `live` partition. The rule always writes pk='live', so this changes
			// nothing in the ordinary case; it is there for the case where a payload carries a
			// `pk` of its own and SELECT * lets it through. That payload could then never reach
			// another partition -- in particular not the viewer slot leases, which share this
			// table -- whatever the SQL does with it.
			conditions: {
				'ForAllValues:StringEquals': { 'dynamodb:LeadingKeys': ['live'] },
			},
		}));

		// Where a failed write goes. Without an error action a rejected PutItem -- a missing
		// key, a throttle, the condition above -- vanishes without a trace.
		const ruleErrors = new cdk.aws_logs.LogGroup(this, 'LiveRuleErrors', {
			retention: cdk.aws_logs.RetentionDays.ONE_MONTH,
			removalPolicy: cdk.RemovalPolicy.DESTROY,
		});
		ruleErrors.grantWrite(ruleRole);

		new cdk.aws_iot.CfnTopicRule(this, 'LiveToTable', {
			topicRulePayload: {
				description: 'Each live message into the five-minute table',
				awsIotSqlVersion: '2016-03-23',
				// timestamp() is IoT's receive time in ms. The message's own `t` stays the sort
				// key: it is the archive record's timestamp, which is what a page de-duplicates on.
				sql: `SELECT *, 'live' AS pk, floor(timestamp() / 1000) + ${LIVE_WINDOW_S} AS ttl `
					+ `FROM '${LIVE_TOPIC}'`,
				ruleDisabled: false,
				actions: [{
					dynamoDBv2: {
						roleArn: ruleRole.roleArn,
						putItem: { tableName: this.table.tableName },
					},
				}],
				errorAction: {
					cloudwatchLogs: { logGroupName: ruleErrors.logGroupName, roleArn: ruleRole.roleArn },
				},
			},
		});


		// ==== the API a page talks to ====================================================
		// Seed (read the last five minutes) and session (admit a viewer to the stream), both
		// through CloudFront so they are same-origin with the page.
		if (!cfg.liveIotEndpoint) {
			throw new Error('cloud.json: live_iot_endpoint is empty, so the live API would hand '
				+ 'out URLs to nowhere. aws iot describe-endpoint --endpoint-type iot:Data-ATS '
				+ `--region ${this.region}`);
		}

		// ---- what a viewer may do, before the session policy narrows it to one id -------
		const liveFnRole = new cdk.aws_iam.Role(this, 'LiveFnRole', {
			assumedBy: new cdk.aws_iam.ServicePrincipal('lambda.amazonaws.com'),
			managedPolicies: [cdk.aws_iam.ManagedPolicy.fromAwsManagedPolicyName(
				'service-role/AWSLambdaBasicExecutionRole')],
		});
		// The ten ids ENUMERATED, not `sigen-view-*`. The cap is the number of ids a viewer
		// can ever connect as, so it is written out where it can be counted.
		const viewerClients = Array.from({ length: VIEWER_SLOTS },
			(_, i) => arn(`client/${VIEWER_CLIENT_PREFIX}${i}`));
		const viewerRole = new cdk.aws_iam.Role(this, 'ViewerRole', {
			assumedBy: new cdk.aws_iam.ArnPrincipal(liveFnRole.roleArn),
			description: 'Subscribe-only access to the live topic, as one of ten client ids',
			maxSessionDuration: cdk.Duration.hours(1),
		});
		viewerRole.addToPolicy(new cdk.aws_iam.PolicyStatement({
			actions: ['iot:Connect'], resources: viewerClients,
		}));
		viewerRole.addToPolicy(new cdk.aws_iam.PolicyStatement({
			actions: ['iot:Subscribe'], resources: [arn(`topicfilter/${LIVE_TOPIC}`)],
		}));
		viewerRole.addToPolicy(new cdk.aws_iam.PolicyStatement({
			actions: ['iot:Receive'], resources: [arn(`topic/${LIVE_TOPIC}`)],
		}));

		// ---- the function ------------------------------------------------------------------
		const liveFn = new cdk.aws_lambda.Function(this, 'LiveFn', {
			runtime: cdk.aws_lambda.Runtime.PYTHON_3_12,
			handler: 'handler.lambda_handler',
			code: ownPackageLambdaCode(path.join(LAMBDA_DIR, 'live')),
			role: liveFnRole,
			memorySize: 256,
			timeout: cdk.Duration.seconds(10),
			environment: {
				TABLE: this.table.tableName,
				VIEWER_ROLE_ARN: viewerRole.roleArn,
				IOT_ENDPOINT: cfg.liveIotEndpoint,
				TOPIC: LIVE_TOPIC,
				CLIENT_PREFIX: VIEWER_CLIENT_PREFIX,
				SLOTS: String(VIEWER_SLOTS),
				PUBLIC_SLOTS: String(PUBLIC_SLOTS),
				WINDOW_S: String(LIVE_WINDOW_S),
				// Same reason as the ingest Lambda's: TZ is reserved, so the handler copies it.
				CAPTURE_TZ: cfg.captureTz,
			},
			logGroup: new cdk.aws_logs.LogGroup(this, 'LiveFnLogs', {
				retention: cdk.aws_logs.RetentionDays.ONE_MONTH,
				removalPolicy: cdk.RemovalPolicy.DESTROY,
			}),
			description: 'Live seed and viewer admission. See cloud/lambda/live/handler.py.',
		});
		// Read both partitions; write only the slot leases. It can no more write a live point
		// than the rule can write a lease.
		liveFnRole.addToPolicy(new cdk.aws_iam.PolicyStatement({
			actions: ['dynamodb:Query'],
			resources: [this.table.tableArn],
			conditions: { 'ForAllValues:StringEquals': { 'dynamodb:LeadingKeys': ['live', 'slot'] } },
		}));
		liveFnRole.addToPolicy(new cdk.aws_iam.PolicyStatement({
			actions: ['dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem'],
			resources: [this.table.tableArn],
			conditions: { 'ForAllValues:StringEquals': { 'dynamodb:LeadingKeys': ['slot'] } },
		}));
		liveFnRole.addToPolicy(new cdk.aws_iam.PolicyStatement({
			actions: ['sts:AssumeRole'], resources: [viewerRole.roleArn],
		}));

		// ==== the device tracker ===========================================================
		// Devices, their states, and every state change someone tapped -- the only thing in this
		// stack that is not a copy of something the archive holds. So: RETAIN, and point-in-time
		// recovery, which for a table this size costs cents.
		//
		// One table, two kinds of partition: CFG holds every device and state (a handful of items,
		// read in one Query), and EV#<device> holds that device's events, sorted by their best
		// time estimate so a window is one Query too. Ids are generated, never derived from names,
		// so renaming a device or a state is one attribute and breaks nothing that refers to it.
		const trackerTable = new cdk.aws_dynamodb.Table(this, 'TrackerTable', {
			partitionKey: { name: 'pk', type: cdk.aws_dynamodb.AttributeType.STRING },
			sortKey: { name: 'sk', type: cdk.aws_dynamodb.AttributeType.STRING },
			billingMode: cdk.aws_dynamodb.BillingMode.PROVISIONED,
			readCapacity: 2,
			writeCapacity: 2,
			pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
			removalPolicy: cdk.RemovalPolicy.RETAIN,
		});

		// The authorizer: the edge gate's check, for the API's own hostname. Same verifier
		// (auth-edge/jwt.js), same generated config (gateConfig), so the two gates are one
		// decision made twice rather than two decisions that could drift.
		const authConfig = gateConfig(cfg);
		const authorizerFn = new cdk.aws_lambda.Function(this, 'AuthorizerFn', {
			runtime: cdk.aws_lambda.Runtime.NODEJS_22_X,
			handler: 'index.handler',
			code: cdk.aws_lambda.Code.fromAsset(path.join(LAMBDA_DIR, 'authorizer'), {
				assetHashType: cdk.AssetHashType.OUTPUT,
				bundling: {
					image: cdk.DockerImage.fromRegistry('scratch'),
					local: {
						tryBundle(outputDir: string): boolean {
							fs.copyFileSync(path.join(LAMBDA_DIR, 'authorizer', 'index.js'),
								path.join(outputDir, 'index.js'));
							fs.copyFileSync(path.join(LAMBDA_DIR, 'auth-edge', 'jwt.js'),
								path.join(outputDir, 'jwt.js'));
							fs.writeFileSync(path.join(outputDir, 'config.js'),
								'// GENERATED AT SYNTH from cloud.json. Do not edit.\n'
								+ 'module.exports = ' + JSON.stringify(authConfig, null, 2) + ';\n');
							return true;
						},
					},
				},
			}),
			memorySize: 128,
			timeout: cdk.Duration.seconds(5),
			logGroup: new cdk.aws_logs.LogGroup(this, 'AuthorizerLogs', {
				retention: cdk.aws_logs.RetentionDays.ONE_MONTH,
				removalPolicy: cdk.RemovalPolicy.DESTROY,
			}),
			description: 'Live API authorizer: the edge gate\'s token and allowlist check',
		});

		const trackerFn = new cdk.aws_lambda.Function(this, 'TrackerFn', {
			runtime: cdk.aws_lambda.Runtime.PYTHON_3_12,
			handler: 'handler.lambda_handler',
			code: cdk.aws_lambda.Code.fromAsset(path.join(LAMBDA_DIR, 'tracker')),
			memorySize: 256,
			timeout: cdk.Duration.seconds(10),
			environment: { TABLE: trackerTable.tableName },
			logGroup: new cdk.aws_logs.LogGroup(this, 'TrackerFnLogs', {
				retention: cdk.aws_logs.RetentionDays.ONE_MONTH,
				removalPolicy: cdk.RemovalPolicy.DESTROY,
			}),
			description: 'Device tracker: devices, states, state-change events. Behind the authorizer.',
		});
		// No Delete: nothing in the tracker is ever deleted, only archived or marked.
		trackerFn.addToRolePolicy(new cdk.aws_iam.PolicyStatement({
			actions: ['dynamodb:GetItem', 'dynamodb:Query', 'dynamodb:PutItem', 'dynamodb:UpdateItem'],
			resources: [trackerTable.tableArn],
		}));

		// ---- HTTP API ------------------------------------------------------------------------
		// API Gateway rather than a function URL for the one thing a function URL lacks:
		// throttling. The session route is public and each call can cost an AssumeRole, so it
		// is the route a stranger could lean on; the stage refuses past these rates with a 429
		// before the function -- or the bill -- sees it.
		const api = new cdk.aws_apigatewayv2.CfnApi(this, 'LiveApi', {
			name: `sigen-live-${this.stackName}`, protocolType: 'HTTP',
		});
		const integrate = (id: string, fn: cdk.aws_lambda.Function) =>
			new cdk.aws_apigatewayv2.CfnIntegration(this, id, {
				apiId: api.ref, integrationType: 'AWS_PROXY',
				integrationUri: fn.functionArn,
				integrationMethod: 'POST', payloadFormatVersion: '2.0',
			});
		const liveIntegration = integrate('LiveIntegration', liveFn);
		const trackerIntegration = integrate('TrackerIntegration', trackerFn);

		const authorizer = new cdk.aws_apigatewayv2.CfnAuthorizer(this, 'Authorizer', {
			apiId: api.ref, name: 'session-cookie', authorizerType: 'REQUEST',
			authorizerUri: `arn:${this.partition}:apigateway:${this.region}:lambda:path/2015-03-31/`
				+ `functions/${authorizerFn.functionArn}/invocations`,
			authorizerPayloadFormatVersion: '2.0',
			enableSimpleResponses: true,
			// No cookie, no invocation: API Gateway answers 401 itself.
			identitySource: ['$request.header.Cookie'],
			// Cached per cookie header, briefly. The allowlist is baked in at deploy, so a cached
			// "yes" can only be as stale as the cookie it was keyed on.
			authorizerResultTtlInSeconds: 60,
		});
		authorizerFn.addPermission('ApiInvokeAuthorizer', {
			principal: new cdk.aws_iam.ServicePrincipal('apigateway.amazonaws.com'),
			sourceArn: `arn:aws:execute-api:${this.region}:${this.account}:${api.ref}/authorizers/`
				+ authorizer.ref,
		});

		// The live routes take the authorizer exactly when the view is locked -- the same
		// cfg.livePublic that gates /live and /api/live/* at the edge (site-stack.ts).
		const liveGated = !cfg.livePublic;
		const routes: Array<[string, string, string, cdk.aws_lambda.Function,
			cdk.aws_apigatewayv2.CfnIntegration, boolean, number, number]> = [
			// [construct id, route key, permission path, function, integration, gated, rate/s, burst]
			['SeedRoute', 'GET /api/live/seed', 'api/live/seed', liveFn, liveIntegration,
				liveGated, 2, 10],
			['SessionRoute', 'POST /api/live/session', 'api/live/session', liveFn, liveIntegration,
				liveGated, 1, 10],
			// The tracker page's own stream: the same handler, but behind the authorizer always,
			// which is what entitles it to the two reserved slots.
			['TrackerLiveRoute', 'POST /api/tracker/live', 'api/tracker/live', liveFn,
				liveIntegration, true, 2, 10],
			['TrackerRoute', 'ANY /api/tracker/{proxy+}', 'api/tracker/*', trackerFn,
				trackerIntegration, true, 5, 20],
		];
		const routeSettings: Record<string, unknown> = {};
		const routeResources: cdk.CfnResource[] = [];
		for (const [id, routeKey, permPath, fn, integration, gated, rate, burst] of routes) {
			routeResources.push(new cdk.aws_apigatewayv2.CfnRoute(this, id, {
				apiId: api.ref, routeKey,
				target: cdk.Fn.join('', ['integrations/', integration.ref]),
				...(gated ? { authorizationType: 'CUSTOM', authorizerId: authorizer.ref } : {}),
			}));
			fn.addPermission(`${id}Invoke`, {
				principal: new cdk.aws_iam.ServicePrincipal('apigateway.amazonaws.com'),
				sourceArn: `arn:aws:execute-api:${this.region}:${this.account}:${api.ref}/*/*/${permPath}`,
			});
			routeSettings[routeKey] = { ThrottlingRateLimit: rate, ThrottlingBurstLimit: burst };
		}
		const stage = new cdk.aws_apigatewayv2.CfnStage(this, 'LiveStage', {
			apiId: api.ref, stageName: '$default', autoDeploy: true,
			// Anything without its own line above -- a route added later and forgotten -- gets the
			// tightest setting rather than the account default of thousands a second.
			defaultRouteSettings: { throttlingRateLimit: 1, throttlingBurstLimit: 5 },
			routeSettings,
		});
		// routeSettings names routes by KEY, a string, so CloudFormation sees no reference and is
		// free to create the stage first -- and the stage is then refused: "Unable to find Route
		// by key POST /api/live/session". The first deploy of this stack failed exactly so.
		for (const r of routeResources) stage.node.addDependency(r);
		this.apiDomain = `${api.ref}.execute-api.${this.region}.amazonaws.com`;
		new cdk.CfnOutput(this, 'LiveApiDomain', {
			value: this.apiDomain,
			description: 'SigenSite reads this across regions; live_api_domain in cloud.json is '
				+ 'the fallback if that reference ever fails',
		});

		new cdk.CfnOutput(this, 'TrackerTableName', { value: trackerTable.tableName });
		new cdk.CfnOutput(this, 'LiveTableName', { value: this.table.tableName });
		new cdk.CfnOutput(this, 'PiPolicyName', {
			value: piPolicy.ref,
			description: cfg.liveCertId
				? 'Attached to live_cert_id'
				: 'NOT attached: set live_cert_id in cloud.json and redeploy',
		});
		new cdk.CfnOutput(this, 'LiveTopic', { value: LIVE_TOPIC });
	}
}
