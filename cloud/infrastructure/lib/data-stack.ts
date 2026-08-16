import * as cdk from 'aws-cdk-lib';
import * as path from 'path';
import { Construct } from 'constructs';
import { CloudConfig } from './config';
import { archiveLambdaCode } from './archive-bundle';

const REPO_ROOT = path.resolve(__dirname, '../../..');

/**
 * The one bucket, and the Lambda that turns raw into tiles.
 *
 * The bucket holds four things that are governed very differently:
 *
 *   raw/    the archive of record. Write-only for the capture host, and NOT reachable
 *           through CloudFront at any path -- the site stack's origin policy names
 *           site/, agg/ and share/, and leaves this one out. It is the only copy of
 *           telemetry that no longer exists anywhere else, so it is versioned and the
 *           logger principal is explicitly denied delete.
 *   agg/    derived tiles. Reproducible from raw at any time, so nothing here is
 *           precious -- which is exactly why the read path is allowed to serve it.
 *   share/  immutable copies made when someone shares a view. Public.
 *   site/   the page.
 *
 * The trust boundary that matters: the capture host's credential can PUT under raw/ and
 * nothing else. It cannot read, list or delete, so a key lifted off a machine sitting in
 * a garage cannot be used to read the archive back, enumerate it, or destroy it. The
 * ingest Lambda is the only thing that reads raw/, and it writes only agg/.
 */
export interface DataStackProps extends cdk.StackProps {
	cfg: CloudConfig;
}

export class DataStack extends cdk.Stack {
	public readonly bucket: cdk.aws_s3.Bucket;
	public readonly logger: cdk.aws_iam.User;

	constructor(scope: Construct, id: string, props: DataStackProps) {
		super(scope, id, props);
		const { cfg } = props;

		this.bucket = new cdk.aws_s3.Bucket(this, 'Bucket', {
			bucketName: cfg.bucket,
			// The archive of record. An overwrite with truncated or wrong bytes is
			// recoverable; without versioning it would not be.
			versioned: true,
			encryption: cdk.aws_s3.BucketEncryption.S3_MANAGED,
			blockPublicAccess: cdk.aws_s3.BlockPublicAccess.BLOCK_ALL,
			enforceSSL: true,
			// RETAIN, and not negotiable: `cdk destroy` must not be able to delete
			// telemetry that exists nowhere else.
			removalPolicy: cdk.RemovalPolicy.RETAIN,
			lifecycleRules: [
				{
					// Raw is read only by ingest, and only for the day around a new
					// arrival, so almost all of it is never read again. IA is cheaper per
					// GB with a 30-day minimum and a per-request retrieval charge that
					// nothing here will trigger at scale.
					id: 'raw-to-infrequent-access',
					prefix: 'raw/',
					transitions: [{
						storageClass: cdk.aws_s3.StorageClass.INFREQUENT_ACCESS,
						transitionAfter: cdk.Duration.days(90),
					}],
				},
				{
					// Versioning is insurance against a bad overwrite, not an archive of
					// its own. Old versions of a tile are worthless the moment a correct
					// one lands.
					id: 'expire-noncurrent-versions',
					noncurrentVersionExpiration: cdk.Duration.days(30),
				},
				{
					id: 'abort-incomplete-uploads',
					abortIncompleteMultipartUploadAfter: cdk.Duration.days(7),
				},
			],
		});

		// ---- the ingest Lambda ------------------------------------------------
		// The archive modules are packaged flat beside the handler, exactly as they sit
		// in the repository: Python puts the handler's directory on sys.path, so
		// `import series` resolves with no package and no PYTHONPATH.
		//
		// The list is read from PACKAGE.txt rather than written here, because a
		// hand-maintained copy was already wrong once -- it omitted config.py, which
		// serve.py imports, and the only symptom was "No module named 'config'" in a
		// CloudWatch log after deploy. tests/test_web.py recomputes the import closure and
		// asserts that file is exactly right, so the failure now happens in the suite.
		// The bundling, the PACKAGE.txt list and the OUTPUT asset hash all live in
		// archive-bundle.ts, because the share Lambda in site-stack.ts needs exactly the
		// same closure -- it imports the same tile geometry rather than restating it.
		const ingestCode = archiveLambdaCode(path.join(REPO_ROOT, 'cloud/lambda/ingest'));

		const ingest = new cdk.aws_lambda.Function(this, 'IngestFn', {
			runtime: cdk.aws_lambda.Runtime.PYTHON_3_12,
			handler: 'handler.lambda_handler',
			code: ingestCode,
			// Measured at ~10 s for the incremental path on two days of archive, and it
			// is constant-time: the day rebuild always reads one day however old the
			// archive gets. 300 s is headroom for a backfill storm, not the steady state.
			timeout: cdk.Duration.seconds(300),
			// Lambda scales CPU with memory and the work is single-threaded Python, so
			// there is nothing to gain above roughly one vCPU.
			memorySize: 1769,
			environment: {
				BUCKET: this.bucket.bucketName,
				RAW_PREFIX: 'raw/',
				AGG_PREFIX: 'agg/',
				// NOT named TZ, which Lambda reserves and CloudFormation refuses to set.
				// The handler copies this into TZ and calls time.tzset() before anything
				// computes a local time -- see its comment. Without it every axis label
				// on the hosted page would be twelve hours out.
				CAPTURE_TZ: cfg.captureTz,
			},
			// NO reserved concurrency, though one-at-a-time is what this wants. A new
			// account's total Lambda concurrency quota is 10, and reserving any of it
			// drops unreserved below the 10 that AWS insists stay free -- the deploy is
			// rejected outright.
			//
			// It does not matter in the steady state: one rotation an hour is one event,
			// so there is nothing to contend with. It matters in a burst, where several
			// invocations rebuild the same day tile from different views of the raw
			// prefix and the last writer wins -- leaving a day tile that is missing the
			// most recent hour until the next rotation rewrites it. Self-healing, and
			// bounded to an hour, but not something to rely on: that is why a backfill
			// computes tiles in one pass and uploads them, and why the handler takes an
			// explicit {"rebuild": "<plan>"} invocation. Set this to 1 if the account's
			// quota is ever raised.
			logGroup: new cdk.aws_logs.LogGroup(this, 'IngestLogs', {
				retention: cdk.aws_logs.RetentionDays.ONE_MONTH,
				removalPolicy: cdk.RemovalPolicy.DESTROY,
			}),
			description: 'Raw archive -> precomputed tiles. See ingest.py.',
		});

		this.bucket.grantRead(ingest, 'raw/*');
		// Read as well as write on agg/: index.json is assembled from every plan's
		// published meta.json, because a single invocation only ever downloads one plan's
		// raw and would otherwise write an index naming just that one. Reading back what
		// it already writes widens nothing.
		this.bucket.grantReadWrite(ingest, 'agg/*');

		this.bucket.addEventNotification(
			cdk.aws_s3.EventType.OBJECT_CREATED,
			new cdk.aws_s3_notifications.LambdaDestination(ingest),
			{ prefix: 'raw/' });

		// ---- the capture host's credential ------------------------------------
		// An IAM user rather than a role: the capture host is a Mac in a garage, and
		// eventually a Pi booting off a USB stick. There is no instance metadata to
		// assume a role from, and a long-lived key that can only append is a smaller
		// risk than the machinery to avoid one.
		this.logger = new cdk.aws_iam.User(this, 'LoggerUser', {
			userName: `${cfg.bucket}-logger`,
		});
		this.logger.addToPolicy(new cdk.aws_iam.PolicyStatement({
			// PutObject only. No Get, no List, no Delete: a key lifted off the capture
			// host cannot read the archive back or enumerate it, which is why sync.py
			// keeps a local ledger of what it has uploaded instead of asking S3.
			actions: ['s3:PutObject'],
			resources: [this.bucket.arnForObjects('raw/*')],
		}));
		// Belt and braces against a future policy edit: deny delete to this principal
		// outright, at the bucket. An identity policy that grows a wildcard cannot
		// override a resource-based Deny.
		this.bucket.addToResourcePolicy(new cdk.aws_iam.PolicyStatement({
			effect: cdk.aws_iam.Effect.DENY,
			principals: [new cdk.aws_iam.ArnPrincipal(this.logger.userArn)],
			actions: ['s3:DeleteObject', 's3:DeleteObjectVersion'],
			resources: [this.bucket.arnForObjects('*')],
		}));

		new cdk.CfnOutput(this, 'BucketName', { value: this.bucket.bucketName });
		new cdk.CfnOutput(this, 'IngestFunctionName', { value: ingest.functionName });
		new cdk.CfnOutput(this, 'LoggerUserName', {
			value: this.logger.userName,
			description: 'Create its access key yourself: aws iam create-access-key ' +
				'--user-name <this> --profile <profile>. Deliberately not a stack output.',
		});
	}
}
