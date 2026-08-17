import * as cdk from 'aws-cdk-lib';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import { Construct } from 'constructs';
import { CloudConfig } from './config';
import { archiveLambdaCode } from './archive-bundle';

const REPO_ROOT = path.resolve(__dirname, '../../..');
const WEB = path.join(REPO_ROOT, 'web');

/**
 * The HTML entry points buildSite() writes with NO file extension, because they answer at
 * /view and the URL is the contract.
 *
 * Named once and shared, because the deployment has to know which objects need their
 * Content-Type declared -- see the two BucketDeployments below. A file added to buildSite()
 * without being added here would be uploaded as binary/octet-stream and would download
 * instead of rendering, which is a symptom nothing about it suggests.
 *
 * There used to be a second, `share-view`, serving every /p/<uid>. It is gone because a share
 * now carries its own page: /p/<uid> serves `share/<uid>/page.html`, copied at share time out of
 * the pinned bundle. Being absent from this list is also what lets the Site deployment prune the
 * object it left behind.
 */
const HTML_ENTRY_POINTS = ['view'] as const;

/** Copied verbatim; their extensions tell S3 what they are. */
const WEB_ASSETS = ['app.js', 'tiles.js', 'charts.js', 'style.css', 'favicon.svg'] as const;

/**
 * Everything a rendered page is made of: the one page, and every file it loads.
 *
 * This is the list the bundle identity below is computed over, so a file the page starts loading
 * without being added here would be a change the version cannot see -- and a share pinned to an
 * unchanged version would then drift after all. `pinAssets()` catches that by deriving the
 * references from the page itself and insisting every one of them was rewritten.
 */
const VIEWER_SOURCES = ['index.html', ...WEB_ASSETS] as const;

/**
 * The identity of the viewer bundle: sha256 over the name and bytes of every file above,
 * truncated to 12 hex.
 *
 * **A hash rather than a counter, so that a redeploy is an overwrite.** Deploying the same web/
 * twice writes the same keys twice, which is idempotent and free; only a real change to the page
 * or the code creates a second bundle. That is what makes it affordable to keep every bundle a
 * share has ever named, forever.
 *
 * Computed in the stack CONSTRUCTOR rather than inside the bundling callback, because it has to
 * reach two places: the object keys buildSite() writes, and the share Lambda's environment. Asset
 * bundling runs during synth, after the construct tree is built, so a value discovered there
 * could not be handed to a function that was already declared.
 */
function viewerVersion(): string {
	const h = crypto.createHash('sha256');
	// Sorted and name-delimited, so the digest depends on the contents rather than on the order
	// this file happens to list them in, and so two files cannot shift bytes across the boundary
	// between them and leave the hash unchanged.
	for (const f of [...VIEWER_SOURCES].sort()) {
		h.update(f, 'utf-8');
		h.update('\0');
		h.update(fs.readFileSync(path.join(WEB, f)));
	}
	return h.digest('hex').slice(0, 12);
}

/**
 * CloudFront, and the decision about what is gated.
 *
 * **The page code is public; the telemetry is not.** app.js, tiles.js, charts.js and
 * style.css are published on GitHub, so gating them would protect nothing -- and it would
 * break the public share pages, which need exactly those files to render. So the gate goes
 * on /view, /agg/* and /api/share, and nothing else.
 *
 * The one control that matters most is negative: the origin access policy names site/,
 * agg/ and share/. `raw/` is ABSENT, so the archive of record is unreachable through
 * CloudFront by any path, gated or not, however the behaviours are later edited. A
 * misconfigured behaviour can expose aggregates, which are derived and reproducible; it
 * cannot expose the raw bytes.
 *
 * Two origins over one bucket, differing only in originPath, so no URL rewriting is needed
 * for the common cases: /view resolves to site/view, /agg/x to agg/x. Only /p/* needs a
 * rewrite, because a share id has to select that share's own page, and that is a CloudFront
 * Function rather than a Lambda@Edge -- it is a string operation, and those behaviours have
 * no gate to run anyway.
 */
export interface SiteStackProps extends cdk.StackProps {
	cfg: CloudConfig;
	edgeVersionArn: string;
	callbackApiDomain: string;
}

export class SiteStack extends cdk.Stack {
	constructor(scope: Construct, id: string, props: SiteStackProps) {
		super(scope, id, props);
		const { cfg } = props;
		const version = viewerVersion();

		const bucket = cdk.aws_s3.Bucket.fromBucketName(this, 'Bucket', cfg.bucket);
		const cert = cdk.aws_certificatemanager.Certificate.fromCertificateArn(
			this, 'Cert', cfg.certificateArn);

		const edge = cdk.aws_lambda.Version.fromVersionArn(
			this, 'AuthEdge', props.edgeVersionArn);
		const gate: cdk.aws_cloudfront.EdgeLambda[] = [{
			functionVersion: edge,
			eventType: cdk.aws_cloudfront.LambdaEdgeEventType.VIEWER_REQUEST,
		}];
		// The same function version, plus the request body -- for /api/share ONLY, because it
		// is the only behaviour with a body and the only one behind a Lambda function URL. The
		// gate has to hash the body into `x-amz-content-sha256` or the OAC's SigV4 signature
		// does not match and Lambda refuses the request 403 without invoking it; see
		// signPayload() in cloud/lambda/auth-edge/index.js. Kept separate from `gate` so /view
		// and /agg/* are not handed a body they have no use for.
		const gateSigningBody: cdk.aws_cloudfront.EdgeLambda[] = [{
			functionVersion: edge,
			eventType: cdk.aws_cloudfront.LambdaEdgeEventType.VIEWER_REQUEST,
			includeBody: true,
		}];

		const siteOrigin = cdk.aws_cloudfront_origins.S3BucketOrigin
			.withOriginAccessControl(bucket, { originPath: '/site' });
		const dataOrigin = cdk.aws_cloudfront_origins.S3BucketOrigin
			.withOriginAccessControl(bucket);
		const authOrigin = new cdk.aws_cloudfront_origins.HttpOrigin(
			props.callbackApiDomain, {
				protocolPolicy: cdk.aws_cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
			});

		// ---- POST /api/share -------------------------------------------------
		// It lives in THIS stack, not the data stack, for two reasons. It needs no
		// cross-stack reference for its origin, which is the coupling that made the read
		// gate's code undeployable (see cdk.json). And the data stack must synth before
		// Cognito exists, while this stack already only synths once the pool ids are in
		// cloud.json.
		const shareFn = new cdk.aws_lambda.Function(this, 'ShareFn', {
			runtime: cdk.aws_lambda.Runtime.PYTHON_3_12,
			handler: 'handler.lambda_handler',
			code: archiveLambdaCode(path.join(REPO_ROOT, 'cloud/lambda/share')),
			// Copying a handful of tiles server-side. The work is S3 round trips, not CPU.
			timeout: cdk.Duration.seconds(60),
			memorySize: 512,
			environment: {
				BUCKET: cfg.bucket,
				AGG_PREFIX: 'agg/',
				SHARE_PREFIX: 'share/',
				SITE_DOMAIN: cfg.domain,
				// Reserved as TZ; see the handler.
				CAPTURE_TZ: cfg.captureTz,
				// Which viewer bundle a share made from now on is pinned to. Passed in rather
				// than discovered by the function, so the page a share renders with is decided
				// by the deploy that published it and cannot move afterwards.
				SITE_PREFIX: 'site/',
				VIEWER_VERSION: version,
			},
			logGroup: new cdk.aws_logs.LogGroup(this, 'ShareLogs', {
				retention: cdk.aws_logs.RetentionDays.ONE_MONTH,
				removalPolicy: cdk.RemovalPolicy.DESTROY,
			}),
			description: 'Freeze one view into share/<uid>/. See cloud/lambda/share.',
		});
		// Read the aggregates, write the shares, and nothing else. Notably NOT raw/: a share
		// is made from tiles, so the archive of record is not in this function's reach.
		shareFn.addToRolePolicy(new cdk.aws_iam.PolicyStatement({
			actions: ['s3:GetObject'],
			resources: [`arn:aws:s3:::${cfg.bucket}/agg/*`],
		}));
		shareFn.addToRolePolicy(new cdk.aws_iam.PolicyStatement({
			actions: ['s3:PutObject', 's3:GetObject'],
			resources: [`arn:aws:s3:::${cfg.bucket}/share/*`],
		}));
		// The pinned viewer bundles, and nothing else under site/. A share copies one page out of
		// `site/v/<version>/` into itself; it has no business with the live entry points beside
		// them, so the grant stops at that prefix.
		shareFn.addToRolePolicy(new cdk.aws_iam.PolicyStatement({
			actions: ['s3:GetObject'],
			resources: [`arn:aws:s3:::${cfg.bucket}/site/v/*`],
		}));
		// ListBucket, for the same reason CloudFront needs it a few lines below: without it
		// S3 answers 403 for a key that does NOT exist, because it will not confirm absence
		// to a caller that cannot list. The share function has to know which tiles are
		// absent -- that is how a gap in the data is represented -- and catching 403 as
		// "absent" would make a broken policy look like an outage. Scoped by prefix, so raw/
		// cannot even be enumerated; list_objects_v2 sends a prefix, which is what makes the
		// condition work here.
		shareFn.addToRolePolicy(new cdk.aws_iam.PolicyStatement({
			actions: ['s3:ListBucket'],
			resources: [`arn:aws:s3:::${cfg.bucket}`],
			conditions: { StringLike: { 's3:prefix': ['agg/*', 'share/*', 'site/v/*'] } },
		}));

		// AWS_IAM, not NONE. The read gate is a Lambda@Edge on a CloudFront behaviour, so
		// anything reachable BESIDE CloudFront is ungated -- and this endpoint mints public
		// copies of private telemetry, so an open function URL would let anyone with the URL
		// publish someone else's data. OAC signs CloudFront's requests with SigV4 and IAM
		// refuses everyone else, which makes the URL itself uninteresting.
		const shareUrl = shareFn.addFunctionUrl({
			authType: cdk.aws_lambda.FunctionUrlAuthType.AWS_IAM,
		});
		const shareOrigin = cdk.aws_cloudfront_origins.FunctionUrlOrigin
			.withOriginAccessControl(shareUrl);

		// /p/<uid> -> that share's OWN page. A CloudFront Function, not a Lambda@Edge: it is
		// string work on a path, it runs before the cache, and it costs about a sixth as much
		// per request.
		//
		// It used to point every share id at one `/share-view` object, which is what made a share
		// drift: that object loads the live /app.js. Each share now holds a copy of the page it
		// was made with, so the id selects the renderer as well as the data. See FINDINGS 35.
		const shareRewrite = new cdk.aws_cloudfront.Function(this, 'ShareRewrite', {
			code: cdk.aws_cloudfront.FunctionCode.fromInline(`
function handler(event) {
  // The share's own page, beside its own tiles. The page still reads the id out of
  // location.pathname to find them -- which is why this rewrite has to leave the browser's
  // URL alone, and why a share link needs no query string and has no fragment to lose.
  var m = event.request.uri.match(/^\\/p\\/([A-Za-z0-9_-]{4,64})\\/?$/);
  if (m) {
    event.request.uri = '/share/' + m[1] + '/page.html';
    return event.request;
  }
  // Anything else under /p/ is not a share id. Answered HERE rather than by a distribution
  // error response: those are distribution-wide, and web/tiles.js reads a 404 as "no tile was
  // published for that span", so mapping 404 to a document would turn a gap in the data into a
  // hard failure across the whole viewer.
  return {
    statusCode: 404, statusDescription: 'Not Found',
    headers: {
      'content-type': { value: 'text/plain; charset=utf-8' },
      'cache-control': { value: 'no-store' }
    },
    body: 'Not a share link. A share URL looks like /p/1a2b3c4d.'
  };
}`),
			comment: 'Serve each share its own page: /p/<uid> -> /share/<uid>/page.html',
		});

		const immutable = cdk.aws_cloudfront.CachePolicy.CACHING_OPTIMIZED;
		// The small documents change every hour and the HTML on every deploy. Honour the
		// origin's Cache-Control rather than overriding it, so ingest.py decides -- it is
		// what knows whether a span has finished.
		const respectOrigin = new cdk.aws_cloudfront.CachePolicy(this, 'RespectOrigin', {
			comment: 'Use the origin Cache-Control; ingest.py sets immutable vs 60s',
			defaultTtl: cdk.Duration.seconds(60),
			minTtl: cdk.Duration.seconds(0),
			maxTtl: cdk.Duration.days(365),
			enableAcceptEncodingGzip: true,
			enableAcceptEncodingBrotli: true,
		});

		const dist = new cdk.aws_cloudfront.Distribution(this, 'Dist', {
			domainNames: [cfg.domain],
			certificate: cert,
			// index.html is a small PUBLIC landing page, not the viewer. An anonymous
			// visitor should get something that explains what this is, rather than a
			// redirect into Google.
			defaultRootObject: 'index.html',
			httpVersion: cdk.aws_cloudfront.HttpVersion.HTTP2_AND_3,
			minimumProtocolVersion: cdk.aws_cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
			// North America and Europe only would be the cheap option and the wrong one:
			// every viewer of this site is in New Zealand.
			priceClass: cdk.aws_cloudfront.PriceClass.PRICE_CLASS_ALL,
			comment: `Sigenergy telemetry viewer (${cfg.domain})`,
			defaultBehavior: {
				// Public: the page code and the landing page. See the class docstring.
				origin: siteOrigin,
				viewerProtocolPolicy:
					cdk.aws_cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
				cachePolicy: respectOrigin,
				compress: true,
			},
			additionalBehaviors: {
				// -- gated --------------------------------------------------
				'/view': {
					origin: siteOrigin,
					viewerProtocolPolicy:
						cdk.aws_cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
					cachePolicy: cdk.aws_cloudfront.CachePolicy.CACHING_DISABLED,
					edgeLambdas: gate,
					compress: true,
				},
				'/agg/*': {
					origin: dataOrigin,
					viewerProtocolPolicy:
						cdk.aws_cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
					// Gating the page and not the data would be theatre: /agg/* IS the
					// telemetry, and anyone who guessed a key could read it.
					cachePolicy: respectOrigin,
					edgeLambdas: gate,
					compress: true,
				},
				'/api/share': {
					origin: shareOrigin,
					viewerProtocolPolicy:
						cdk.aws_cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
					// ALLOW_ALL because it is a POST, and CloudFront refuses methods a
					// behaviour does not list -- before the gate ever runs, so the symptom
					// would be a 403 with nothing in any log.
					//
					// A 403 with nothing in any log has a SECOND cause here, and it is the
					// one that actually happened: OAC signs this request with SigV4, and a
					// Lambda function URL rejects an unsigned payload, so a POST body must
					// arrive with its hash in `x-amz-content-sha256`. `gateSigningBody`
					// above is what puts it there. Both halves are required -- includeBody
					// without the header, or the header without includeBody, is 403.
					allowedMethods: cdk.aws_cloudfront.AllowedMethods.ALLOW_ALL,
					cachePolicy: cdk.aws_cloudfront.CachePolicy.CACHING_DISABLED,
					// The gate has to see the cookie, and the function has to see the body.
					// EXCEPT_HOST_HEADER matters for a function URL: SigV4 is computed over
					// the origin's host, so forwarding the viewer's would break the signature.
					// It also carries the header the gate adds; a policy that named headers
					// individually would have to name that one too.
					originRequestPolicy: cdk.aws_cloudfront.OriginRequestPolicy
						.ALL_VIEWER_EXCEPT_HOST_HEADER,
					edgeLambdas: gateSigningBody,
				},
				// -- public -------------------------------------------------
				'/auth/*': {
					origin: authOrigin,
					viewerProtocolPolicy:
						cdk.aws_cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
					// It sets a cookie and must never be cached, and it needs the query
					// string -- the OAuth code and state arrive in it.
					cachePolicy: cdk.aws_cloudfront.CachePolicy.CACHING_DISABLED,
					originRequestPolicy: cdk.aws_cloudfront.OriginRequestPolicy
						.ALL_VIEWER_EXCEPT_HOST_HEADER,
					allowedMethods: cdk.aws_cloudfront.AllowedMethods.ALLOW_ALL,
				},
				'/p/*': {
					// dataOrigin, NOT siteOrigin: the rewrite above resolves to
					// `share/<uid>/page.html`, and an origin with originPath /site would look
					// for `site/share/<uid>/…`. The bucket policy already grants share/*.
					origin: dataOrigin,
					viewerProtocolPolicy:
						cdk.aws_cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
					// A share's page is written once and never rewritten, so it is cacheable on
					// the same terms as its tiles.
					cachePolicy: immutable,
					functionAssociations: [{
						function: shareRewrite,
						eventType: cdk.aws_cloudfront.FunctionEventType.VIEWER_REQUEST,
					}],
					compress: true,
				},
				'/share/*': {
					// A share is a deliberate, immutable copy. Public by design: that is
					// what makes a link sendable to someone with no account.
					origin: dataOrigin,
					viewerProtocolPolicy:
						cdk.aws_cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
					cachePolicy: immutable,
					compress: true,
				},
			},
		});

		// ---- what CloudFront may INVOKE -----------------------------------
		// `withOriginAccessControl()` above grants lambda:InvokeFunctionUrl and stops there.
		// That was sufficient once. Since **October 2025** a function URL requires BOTH
		// lambda:InvokeFunctionUrl and lambda:InvokeFunction, and this one was created after
		// that -- so CloudFront authenticated successfully and was then refused, which is a
		// 403 whose body is a link to urls-auth.html. Nothing invokes, so nothing logs: not in
		// the share function, and the gate one hop earlier has already said `allow`. It cost
		// two rounds of debugging. aws-cdk-lib 2.265.0 adds both actions for authType NONE
		// (aws-lambda/lib/function-url.js) and only the one for OAC, so this fills that gap;
		// a later CDK that grants it too is harmless, the statements are identical.
		//
		// SourceArn, not lambda:InvokedViaFunctionUrl, because SourceArn is what actually
		// contains this: only CloudFront acting for THIS distribution can invoke. The OAC
		// documentation's own command omits the condition key, and it is a security boundary
		// worth stating in the terms the docs use rather than improving on untested.
		shareFn.addPermission('CloudFrontInvokeFunction', {
			principal: new cdk.aws_iam.ServicePrincipal('cloudfront.amazonaws.com'),
			action: 'lambda:InvokeFunction',
			sourceArn: `arn:aws:cloudfront::${this.account}:distribution/`
				+ dist.distributionId,
		});

		// ---- what CloudFront may read -------------------------------------
		// OAC generates a bucket policy automatically, but only for the prefixes it knows
		// about, and `fromBucketName` gives an IBucket the stack cannot attach one to. So
		// it is written explicitly -- which is better anyway: the list of readable prefixes
		// is the security boundary and belongs where it can be read.
		new cdk.aws_s3.CfnBucketPolicy(this, 'OacPolicy', {
			bucket: cfg.bucket,
			policyDocument: {
				Version: '2012-10-17',
				Statement: [{
					Sid: 'CloudFrontReadSitePublishedPrefixesOnly',
					Effect: 'Allow',
					Principal: { Service: 'cloudfront.amazonaws.com' },
					Action: 's3:GetObject',
					// raw/ is DELIBERATELY ABSENT. The archive of record is unreachable
					// through CloudFront at any path, however these behaviours are later
					// edited. Aggregates are derived and reproducible; the raw bytes are not.
					Resource: [
						`arn:aws:s3:::${cfg.bucket}/site/*`,
						`arn:aws:s3:::${cfg.bucket}/agg/*`,
						`arn:aws:s3:::${cfg.bucket}/share/*`,
					],
					Condition: {
						StringEquals: {
							'AWS:SourceArn': `arn:aws:cloudfront::${this.account}:distribution/`
								+ dist.distributionId,
						},
					},
				}, {
					// So a MISSING object returns 404 rather than 403.
					//
					// Without s3:ListBucket, S3 answers AccessDenied for a key that does not
					// exist -- it will not confirm absence to a caller that cannot list. But
					// an absent tile is how web/tiles.js learns the logger was off for that
					// span: ingest.py writes nothing for a span with no records. A 403 there
					// would surface as a hard error and take the whole page down over a gap
					// in the data, and tiles.js deliberately does NOT treat 403 as absent,
					// because on a gated path a 403 is a refusal and must not read as "no
					// data".
					//
					// This grants nothing a viewer can use: CloudFront never exposes a
					// listing, and the SourceArn condition ties it to this distribution.
					Sid: 'CloudFrontDistinguishAbsentFromForbidden',
					Effect: 'Allow',
					Principal: { Service: 'cloudfront.amazonaws.com' },
					Action: 's3:ListBucket',
					Resource: `arn:aws:s3:::${cfg.bucket}`,
					Condition: {
						StringEquals: {
							'AWS:SourceArn': `arn:aws:cloudfront::${this.account}:distribution/`
								+ dist.distributionId,
						},
						StringLike: { 's3:prefix': ['site/*', 'agg/*', 'share/*'] },
					},
				}],
			},
		});

		// ---- the page -----------------------------------------------------
		// TWO deployments over one prefix, and the reason is content type.
		//
		// /view and /p/<uid> must be reachable at those exact URLs, so the objects behind
		// them have NO file extension -- and BucketDeployment derives Content-Type from the
		// extension (it is `aws s3 sync` underneath, which falls back to
		// binary/octet-stream for anything it cannot recognise). The result was a browser
		// DOWNLOADING a file called `view` instead of rendering the viewer, while curl
		// reported a cheerful 200. The gate, the tiles and the page were all fine.
		//
		// contentType is per-deployment, not per-file, so the extensionless entry points
		// get their own deployment that declares it. Splitting on that boundary rather
		// than renaming to view.html is deliberate: /view already carries the Lambda@Edge
		// gate on viewer-request, and CloudFront forbids a CloudFront Function on the same
		// event type for the same behaviour, so there is nowhere to put a rewrite.
		const webSource = cdk.aws_s3_deployment.Source.asset(WEB, {
			assetHashType: cdk.AssetHashType.OUTPUT,
			bundling: {
				image: cdk.DockerImage.fromRegistry('scratch'),
				local: { tryBundle: (out: string) => buildSite(out, cfg, version) },
			},
		});
		const siteBucket = cdk.aws_s3.Bucket.fromBucketName(this, 'SiteBucket', cfg.bucket);

		// Everything whose extension already tells S3 what it is.
		new cdk.aws_s3_deployment.BucketDeployment(this, 'Site', {
			destinationBucket: siteBucket,
			destinationKeyPrefix: 'site',
			sources: [webSource],
			// This deployment owns pruning, so `exclude` here is load-bearing twice over:
			// it keeps the entry points out of THIS upload, and it withholds them from
			// `--delete`. Without it, whichever deployment ran last would delete the
			// other's objects.
			//
			// `v/*` is here for the second reason only, and it is the line the whole pinning
			// arrangement rests on. Those bundles are not in this source -- each was deployed
			// and forgotten -- so without withholding them from `--delete`, this deploy removes
			// the renderer that every existing share names. The symptom is not a failed build:
			// it is links sent months ago going blank.
			exclude: [...HTML_ENTRY_POINTS, 'v/*'],
			// Only this prefix, so a deployment cannot reach raw/ or agg/.
			prune: true,
			distribution: dist,
			distributionPaths: ['/index.html', '/app.js', '/tiles.js', '/charts.js',
				'/style.css', '/favicon.svg'],
		});

		// The extensionless HTML entry points, told what they are.
		new cdk.aws_s3_deployment.BucketDeployment(this, 'SiteEntryPoints', {
			destinationBucket: siteBucket,
			destinationKeyPrefix: 'site',
			sources: [webSource],
			exclude: ['*'],
			include: [...HTML_ENTRY_POINTS],
			contentType: 'text/html; charset=utf-8',
			// The Site deployment above prunes; two deployments both passing --delete over
			// one prefix would race to remove each other's work.
			prune: false,
			distribution: dist,
			// Only /view. A share's page is immutable and lives under /share/<uid>/, so there is
			// no shared entry point behind /p/* left to invalidate.
			distributionPaths: ['/view'],
		});

		// The pinned bundle, for a share to copy its page out of. Every object here is immutable
		// and content-addressed, so this deployment neither prunes nor invalidates: there is
		// nothing to remove -- an existing share still names an older bundle -- and nothing to
		// invalidate, since a changed page produces a changed key.
		const bundle = new cdk.aws_s3_deployment.BucketDeployment(this, 'SiteViewerBundle', {
			destinationBucket: siteBucket,
			destinationKeyPrefix: 'site',
			sources: [webSource],
			exclude: ['*'],
			include: ['v/*'],
			prune: false,
			cacheControl: [cdk.aws_s3_deployment.CacheControl.fromString(
				'public, max-age=31536000, immutable')],
		});
		// So the bundle exists before the function that names it. CloudFormation does not order a
		// Lambda environment update against a custom resource, and in the other order a share
		// created in the seconds between them would copy a page that is not there yet -- which
		// _require() reports as a refusal, but a refused share is still a failed share.
		shareFn.node.addDependency(bundle);

		new cdk.CfnOutput(this, 'DistributionDomain', {
			value: dist.distributionDomainName,
			description: 'CNAME target: point the site subdomain here at your registrar',
		});
		new cdk.CfnOutput(this, 'SiteUrl', { value: `https://${cfg.domain}/view` });
		new cdk.CfnOutput(this, 'ViewerVersion', {
			value: version,
			description: 'The viewer bundle shares created by this deployment are pinned to',
		});
	}
}

/**
 * The three HTML entry points, built from the ONE page in web/ -- plus one immutable,
 * content-addressed copy of the whole bundle for shares to pin themselves to.
 *
 * They differ by a single inline script that sets window.SIGEN_SOURCE before app.js loads.
 * That is the entire difference between the local viewer, the hosted viewer and a frozen
 * share -- there is no second copy of the page, and web/index.html stays the file serve.py
 * serves, so the two cannot drift.
 *
 * **Why a second, pinned copy exists.** A share copies its tiles rather than pointing at them,
 * so that re-aggregating history cannot change what someone was sent -- the share handler says
 * so at length. The RENDERER was not copied, and it is overwritten in place on every deploy:
 * /p/<uid> loaded /app.js, /charts.js and /style.css from the bucket root. So a share was frozen
 * in its data and live in its code, and a link sent in August was drawn by whatever existed when
 * it was opened. The bundle below is the other half of that promise.
 */
function buildSite(out: string, cfg: CloudConfig, version: string): boolean {
	const page = fs.readFileSync(path.join(WEB, 'index.html'), 'utf-8');
	const ANCHOR = '<script src="/tiles.js"></script>';
	if (!page.includes(ANCHOR)) {
		// Loudly, at synth. A silent miss would deploy a page that renders unstyled and
		// fetches nothing, and the only symptom would be a blank viewer.
		throw new Error(`web/index.html no longer contains ${ANCHOR}, so the hosted pages `
			+ `cannot be built from it. Update site-stack.ts.`);
	}
	const withSource = (js: string) =>
		page.replace(ANCHOR, ANCHOR + '\n<script>' + js + '</script>');

	for (const f of WEB_ASSETS) {
		fs.copyFileSync(path.join(WEB, f), path.join(out, f));
	}

	// The gated viewer. Its data comes from /agg/, which the gate also covers, and `share`
	// is what turns on the "Save this view" card -- only here. serve.py's local page has no
	// share endpoint, and a frozen share cannot re-share itself.
	fs.writeFileSync(path.join(out, 'view'),
		withSource(`window.SIGEN_SOURCE={kind:"tiles",base:"/agg/",share:"/api/share"};`));

	// A frozen share. The id comes out of the path, so ONE page answers for all of them, and its
	// data comes from /share/<id>/ -- public, and a copy, so re-aggregating history can never
	// change what someone was sent.
	//
	// It is written ONLY into the pinned bundle below. There is deliberately no `share-view`
	// beside `view` any more: that object was the one thing every share had in common, and
	// therefore the one thing that moved under them. The share Lambda copies this page into each
	// share as `page.html`, and /p/<uid> serves that copy.
	const sharePage = withSource(
		`(function(){var m=location.pathname.match(/^\\/p\\/([A-Za-z0-9_-]{4,64})/);`
		+ `if(!m){document.title="Not a share link";return;}`
		+ `window.SIGEN_SOURCE={kind:"tiles",base:"/share/"+m[1]+"/",frozen:true};})();`);

	// Under a key named for the bundle's contents, with every asset reference pinned. The share
	// Lambda copies THIS file byte for byte, so what a share renders with is settled the moment
	// it is made.
	//
	// A `.html` extension deliberately, unlike the entry point beside it: nothing has to route to
	// this name, so it can carry the extension that tells S3 what it is instead of needing a
	// place in HTML_ENTRY_POINTS.
	const pinned = path.join(out, 'v', version);
	fs.mkdirSync(pinned, { recursive: true });
	fs.writeFileSync(path.join(pinned, 'share-view.html'), pinAssets(sharePage, version));
	for (const f of WEB_ASSETS) {
		fs.copyFileSync(path.join(WEB, f), path.join(pinned, f));
	}

	fs.writeFileSync(path.join(out, 'index.html'), landingPage(cfg));

	// Every extensionless entry point must be one this stack knows to declare a Content-Type
	// for, or it deploys as binary/octet-stream and downloads instead of rendering. Checked
	// at synth, because the alternative is finding out in a browser.
	for (const f of HTML_ENTRY_POINTS) {
		if (!fs.existsSync(path.join(out, f))) {
			throw new Error(`buildSite() did not write the entry point "${f}" that `
				+ `HTML_ENTRY_POINTS promises. The two lists have drifted.`);
		}
	}
	for (const f of fs.readdirSync(out)) {
		// Directories are exempt: `v/` holds the pinned bundles, whose files all carry
		// extensions. This check is about OBJECTS whose key would arrive without a Content-Type.
		if (fs.statSync(path.join(out, f)).isDirectory()) continue;
		if (!path.extname(f) && !(HTML_ENTRY_POINTS as readonly string[]).includes(f)) {
			throw new Error(`buildSite() wrote "${f}", which has no file extension and is `
				+ `not in HTML_ENTRY_POINTS. It would deploy as binary/octet-stream and a `
				+ `browser would download it instead of rendering it. Add it to that list.`);
		}
	}
	return true;
}

/**
 * The page with every root-absolute asset reference moved into `/v/<version>/`.
 *
 * Derived from the page by regex rather than from a list of filenames, for the same reason the
 * bundle identity is: a page that starts loading a sixth file must not be able to half-pin
 * itself. Every reference found is rewritten, and then every one is checked to be gone -- because
 * a missed rewrite is invisible. The share would render perfectly today, load the live /app.js
 * tomorrow, and be exactly the drift this exists to prevent.
 *
 * The inline SIGEN_SOURCE script is untouched: it names no src or href, and its `/share/<id>/`
 * base is data rather than an asset -- a share's tiles are its own and belong to no bundle.
 */
function pinAssets(html: string, version: string): string {
	const refs = [...new Set(Array.from(html.matchAll(/(?:src|href)="(\/[^"]*)"/g),
		(m) => m[1]))];
	if (!refs.length) {
		throw new Error('buildSite() found no root-absolute asset references in web/index.html, '
			+ 'so there is nothing to pin -- which means the page now loads its code some other '
			+ 'way and pinAssets() has silently stopped doing anything.');
	}
	let out = html;
	// Matched with its quotes, so one reference cannot be rewritten through another's prefix.
	for (const ref of refs) out = out.split(`"${ref}"`).join(`"/v/${version}${ref}"`);
	for (const ref of refs) {
		if (out.includes(`"${ref}"`)) {
			throw new Error(`buildSite() left "${ref}" unpinned in the frozen share page. It `
				+ `would load the live asset, so the share would drift on the next deploy.`);
		}
	}
	return out;
}

/** The public landing page. Deliberately says what this is and what it is not. */
function landingPage(cfg: CloudConfig): string {
	return `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SigenStor telemetry</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
  body{font:16px/1.6 system-ui,-apple-system,sans-serif;margin:0;padding:12vh 1.5rem;
       color:#1c1c1c;background:#fbfbfa}
  main{max-width:34em;margin:0 auto}
  h1{font-size:1.4rem;margin:0 0 .25rem}
  p.sub{color:#666;margin:0 0 2rem}
  a.btn{display:inline-block;background:#1c1c1c;color:#fff;text-decoration:none;
        padding:.55rem 1.1rem;border-radius:6px;font-size:.95rem}
  ul{color:#444;padding-left:1.2em}
  footer{margin-top:3rem;color:#888;font-size:.85rem}
  @media(prefers-color-scheme:dark){body{background:#151515;color:#e8e8e8}
    p.sub,ul{color:#aaa}a.btn{background:#e8e8e8;color:#151515}footer{color:#777}}
</style></head><body><main>
  <h1>SigenStor telemetry</h1>
  <p class="sub">Read-only solar and battery data from one house, captured over Modbus.</p>
  <p><a class="btn" href="/view">Open the viewer</a></p>
  <ul>
    <li>The viewer is shared with a few specific people and needs a Google sign-in.</li>
    <li>Individual views can be shared publicly as <code>/p/&lt;id&gt;</code> links, which
        need no account.</li>
    <li>Nothing here can change anything: the capture is read-only by construction, and
        this site only ever reads what it already recorded.</li>
  </ul>
  <footer>Data is up to an hour behind — it is published when the logger rotates its
  archive. Source: <a href="https://github.com/zachmueller/sigen-modbus-logger">
  sigen-modbus-logger</a>.</footer>
</main></body></html>
`;
}
