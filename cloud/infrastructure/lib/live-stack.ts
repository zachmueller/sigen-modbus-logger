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
import { Construct } from 'constructs';
import { CloudConfig } from './config';

/** The one topic. live.py's live_topic default in config.py must equal it; a test checks. */
export const LIVE_TOPIC = 'sigen/live';
/** The Pi's MQTT client id. config.py's live_client_id default; a test checks. */
export const PI_CLIENT_ID = 'sigen-pi';
/** How long a message stays in the table: the width of the live chart. */
export const LIVE_WINDOW_S = 300;

export interface LiveStackProps extends cdk.StackProps {
	cfg: CloudConfig;
}

export class LiveStack extends cdk.Stack {
	readonly table: cdk.aws_dynamodb.Table;

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
