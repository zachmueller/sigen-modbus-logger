import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { CloudConfig } from './config';

/**
 * The account's audit trail, and nothing else.
 *
 * Its own stack because it shares nothing with the rest of the app -- no bucket, no
 * function, no reference in either direction. A trail that lived in DataStack would be
 * redeployed every time the ingest Lambda changed, and a rollback there would take the
 * audit log with it. It can be deployed first or last; there is no ordering constraint.
 *
 * WHAT THIS DOES AND DOES NOT CATCH. Management events only: who called CreateUser,
 * PutBucketPolicy, UpdateDistribution, AssumeRole. It does NOT record object-level reads
 * and writes, which are "data events" and are billed per event. That exclusion is the
 * reason this is nearly free, and it is also why the hourly uploader is invisible here --
 * its PutObject under raw/ is a data event. The question this answers is "what changed in
 * this account, and who changed it", not "who touched which file".
 *
 * Turning data events on for the telemetry bucket would be the obvious next thing to want,
 * and it is a deliberate no: the ingest Lambda reads raw/ and writes agg/ on every
 * rotation, so the volume would be dominated by our own traffic at $0.10 per 100,000
 * events, and the signal -- an unexpected principal reading the archive -- is already
 * covered better by the IAM shape. The capture host's key cannot GET or LIST at all
 * (data-stack.ts), so there is no read to log.
 *
 * COST. The first copy of management events is free, per account, forever. What is left is
 * S3 storage for the log files and the file-validation digests, which on an account this
 * quiet is a few megabytes a month -- cents against the $10 budget alarm. There is
 * deliberately no CloudWatch Logs destination: ingestion is $0.50/GB and nothing here
 * alarms on trail contents, so it would be paying to duplicate the same bytes.
 *
 * THE LIMIT WORTH STATING. This is a single-account trail, so the administrator who could
 * delete it is the same administrator it is watching. A trail cannot be made tamper-proof
 * from inside the account it audits. The real fix is an *organization* trail owned by the
 * management account (there is an org; see the handoff), which a member-account admin
 * cannot disable or erase -- but that has to be deployed from the management account, and
 * this app is pinned to one account id by design. This trail is the cheap 90% of that:
 * enough to reconstruct what happened, not enough to survive a determined insider.
 */
export interface AuditStackProps extends cdk.StackProps {
	cfg: CloudConfig;
}

export class AuditStack extends cdk.Stack {
	public readonly trail: cdk.aws_cloudtrail.Trail;

	constructor(scope: Construct, id: string, props: AuditStackProps) {
		super(scope, id, props);
		const { cfg } = props;

		// Derived from the telemetry bucket's name rather than written out, for the same
		// reason as the logger IAM user: S3 bucket names are global, so a unique one has to
		// carry the account id, and an account id has no business in a public repository.
		const logs = new cdk.aws_s3.Bucket(this, 'TrailBucket', {
			bucketName: `${cfg.bucket}-audit`,
			encryption: cdk.aws_s3.BucketEncryption.S3_MANAGED,
			blockPublicAccess: cdk.aws_s3.BlockPublicAccess.BLOCK_ALL,
			enforceSSL: true,
			// SSE-S3, not KMS. A customer-managed key is $1/month before a single request,
			// which on a bill running at about $1/month total would be the most expensive
			// thing in the account -- to protect log files that are already encrypted at
			// rest and readable only by an account administrator.
			//
			// RETAIN: an audit log that `cdk destroy` erases is not evidence of anything.
			// The cost of leaving a few megabytes behind after a teardown is not a reason.
			removalPolicy: cdk.RemovalPolicy.RETAIN,
			lifecycleRules: [
				{
					// A year, then gone. Long enough to reconstruct anything anyone will
					// actually ask about on a personal account; short enough that storage
					// never becomes a line item.
					id: 'expire-trail-logs',
					expiration: cdk.Duration.days(365),
				},
				{
					// NO transition to Infrequent Access, which is the reflex and is wrong
					// here. IA bills every object at a 128 KB minimum, and CloudTrail log
					// files are a few KB each -- so moving them to a cheaper storage class
					// would multiply the bill for this bucket rather than reduce it. The
					// telemetry bucket transitions raw/ to IA because those objects are
					// megabytes; these are not.
					id: 'abort-incomplete-uploads',
					abortIncompleteMultipartUploadAfter: cdk.Duration.days(7),
				},
			],
		});

		this.trail = new cdk.aws_cloudtrail.Trail(this, 'Trail', {
			trailName: 'sigen-management-events',
			bucket: logs,
			// Multi-region, and this is the one setting not to economise on. Every stack in
			// this app is us-east-1, so a region-scoped trail would be blind in exactly the
			// place an unwanted resource would appear -- somebody else's compute in
			// ap-southeast-1 is the thing you want a new account to tell you about. It also
			// picks up the read gate, which runs as Lambda@Edge wherever the viewer is and
			// logs to ap-southeast-2 for New Zealand traffic.
			isMultiRegionTrail: true,
			// IAM, STS, CloudFront and ACM are global services that emit into us-east-1.
			// Without this the trail would miss every credential and certificate event,
			// which is most of what matters here.
			includeGlobalServiceEvents: true,
			// Reads as well as writes. On a busy account this is the setting to reconsider,
			// because Describe/List calls are the bulk of the volume; on this one they are a
			// handful a day and they are the recon signal -- somebody enumerating the
			// account looks like reads, not writes.
			managementEvents: cdk.aws_cloudtrail.ReadWriteType.ALL,
			// Digest files, so a log that was altered after the fact can be shown to have
			// been. Free, and the whole point of keeping logs at all.
			enableFileValidation: true,
		});

		new cdk.CfnOutput(this, 'TrailArn', { value: this.trail.trailArn });
		new cdk.CfnOutput(this, 'TrailBucketName', { value: logs.bucketName });
	}
}
