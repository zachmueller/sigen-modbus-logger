import * as cdk from 'aws-cdk-lib';
import * as fs from 'fs';
import * as path from 'path';

/**
 * Packaging the archive modules for a Python Lambda.
 *
 * Two Lambdas ship the same set: the ingest function, which turns raw into tiles, and the
 * share function, which copies tiles into an immutable public prefix. Both import `series`,
 * `ingest` and `tiles` rather than restating tile geometry, so both need that import
 * closure beside their handler.
 *
 * The list lives in cloud/lambda/ingest/PACKAGE.txt, not here, because a hand-maintained
 * copy was already wrong once -- it omitted config.py, which serve.py imports, and the only
 * symptom was "No module named 'config'" in a CloudWatch log after a clean deploy.
 * tests/test_web.py recomputes the transitive closure of both handlers and asserts the file
 * matches, so adding an import that reaches a new module fails the suite rather than the
 * Lambda.
 */
const REPO_ROOT = path.resolve(__dirname, '../../..');
const PACKAGE_LIST = path.join(REPO_ROOT, 'cloud/lambda/ingest/PACKAGE.txt');

/** One path per line, ignoring blanks and #-comments. */
export function readPackageList(file: string = PACKAGE_LIST): string[] {
	const lines: string[] = fs.readFileSync(file, 'utf-8').split('\n');
	const out = lines.map((l) => l.trim()).filter((l) => l && !l.startsWith('#'));
	if (!out.length) throw new Error(`${file} lists no files to package`);
	return out;
}

/**
 * `handlerDir`'s own files plus the archive modules, flat, exactly as they sit in the
 * repository -- Python puts the handler's directory on sys.path, so `import series`
 * resolves with no package and no PYTHONPATH. Same arrangement as the capture host.
 *
 * assetHashType OUTPUT is not a detail. CDK fingerprints the asset SOURCE by default, which
 * here is just the handler and PACKAGE.txt -- so editing series.py or ingest.py produced
 * "no changes" and left the old code running in the Lambda. Silently deploying stale code is
 * the worst failure available in a deploy tool, so the hash has to cover what actually
 * ships.
 */
export function archiveLambdaCode(handlerDir: string): cdk.aws_lambda.Code {
	const files = readPackageList();
	return cdk.aws_lambda.Code.fromAsset(handlerDir, {
		assetHashType: cdk.AssetHashType.OUTPUT,
		// A local bundling step rather than a Docker image: the "build" is a handful of file
		// copies, so synth stays fast and needs no container runtime.
		bundling: {
			image: cdk.DockerImage.fromRegistry('scratch'),
			local: {
				tryBundle(outputDir: string): boolean {
					for (const f of files) {
						fs.copyFileSync(path.join(REPO_ROOT, f), path.join(outputDir, f));
					}
					for (const f of fs.readdirSync(handlerDir)) {
						if (f.endsWith('.py')) {
							fs.copyFileSync(path.join(handlerDir, f), path.join(outputDir, f));
						}
					}
					return true;
				},
			},
		},
	});
}
