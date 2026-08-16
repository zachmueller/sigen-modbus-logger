# The hosted viewer

The archive lives on one machine on one LAN. This is the other copy of it, and the way
three other people get to look at it.

Everything here is deploy-time only. `log.py`, `decode.py`, `series.py` and `serve.py`
stay stdlib-only Python with nothing to install — the Node toolchain in
`infrastructure/` runs on a workstation when infrastructure changes, and never on the
capture host.

## The shape of it

```
capture host  ──PutObject raw/* only──▶  S3  ──ObjectCreated──▶  ingest Lambda
                                          │                        │
                                          │   ◀── writes agg/ ─────┘
                                          ▼
                                     CloudFront  ──▶  browser
```

No persistent compute and no database. The page fetches **precomputed tiles** as static
objects, so a read costs an edge-cached GET rather than a decode: `series.py` already
keys buckets on absolute epoch and takes widths from a fixed ladder, which is what makes
per-hour tiles concatenate instead of needing re-aggregation.

One renderer. `web/app.js` reaches the outside world through a single `getJSON()` seam,
and a hosted deployment points that seam at tiles by setting `window.SIGEN_SOURCE`
before the script loads. The page here is byte-for-byte the page `serve.py` serves.

| Path | Gate |
|---|---|
| `/view` | Google sign-in, email allowlist |
| `/agg/*` (tiles) | same gate — a gate on the HTML alone would leave the data open |
| `/p/{uid}`, `/share/*` | **public** — a shared view, copied at share time |
| `/auth/*` | public; `/auth/callback` is what sets the cookie |
| everything else | public: `index.html`, `app.js`, `tiles.js`, `charts.js`, `style.css` |

There is no `/login`. The gate redirects straight to the Cognito Hosted UI, so there is
nothing for such a path to do.

**`/view` and `/p/{uid}` are objects with no file extension**, because the URL is the
contract. `BucketDeployment` derives `Content-Type` from the extension, so they are deployed
by a *second* deployment that declares `text/html` explicitly — see `site-stack.ts`. Get
this wrong and the browser downloads a file called `view` while `curl` reports a cheerful
`200`.

## Configuration

`cloud.json` at the repository root. **Not committed** — it holds an account id, a
domain, a Google client id and other people's email addresses, and this repository is
public. `cloud.example.json` documents every key with placeholder values and is
committed. `cdk.out/` is gitignored too: synthesized templates embed those values.

The one real secret, the Google OAuth client secret, is **never stored in this tree** — it
is passed as a `NoEcho` CloudFormation parameter and re-entered on each deploy, and the
local copy at `cloud/.google-secret` is gitignored and `chmod 600`.

It is not, however, unrecoverable: Cognito has to hold it to perform the token exchange, and
an account administrator can read it back in plaintext with

```sh
aws cognito-idp describe-identity-provider --user-pool-id <pool> --provider-name Google \
    --query 'IdentityProvider.ProviderDetails.client_secret'
```

That is unavoidable and worth knowing rather than being surprised by. It also sets the
rotation procedure: rotate in the Google console, update `cloud/.google-secret`, **and
redeploy `SigenAuthPool`** — the value in Cognito does not change by itself.

AWS credentials come from a named profile (`profile` in `cloud.json`), so nothing
sensitive sits in this tree.

## Deploying

```sh
cd cloud/infrastructure
npm install
npx cdk bootstrap aws://<account>/us-east-1 --profile <profile>
```

Everything is `us-east-1`. Not a preference: Lambda@Edge can only be published there and
CloudFront can only attach a certificate from there, so using one region throughout
means no stack ever references another across a region boundary.

### DNS: two CNAMEs, added by hand

There is no DNS stack, and no Route53 hosted zone. The site lives on a subdomain of a
zone we do not host, at a registrar (Hover) whose DNS editor **has no `NS` record type** —
so there is nothing to delegate to. Two `CNAME` records do the whole job, which is all a
subdomain on CloudFront ever needs:

| Hostname (relative to the parent) | Points at | Purpose |
|---|---|---|
| `_<token>.solar` | `_<token>.acm-validations.aws` | ACM validation. **Must stay** — ACM re-checks it on every renewal, which it attempts ~60 days before the certificate expires. Delete it and renewal fails silently then; check `NotAfter` to know when that is. |
| `solar` | `d<id>.cloudfront.net` | the site |

The parent zone's own records — apex `A`, `MX`, SPF, DKIM, DMARC, `TXT` — are never read,
written or moved by anything here. Nothing about mail or the parent's website is in the
blast radius, which is the point: migrating a zone that fronts a live site *and* Google
Workspace mail, in order to publish one subdomain, is a bad trade.

Capture the parent's records before touching anything, so there is a before/after to diff:

```sh
for t in A AAAA MX TXT CAA NS; do echo "== $t"; dig +short $t <parent-domain>; done
```

**The certificate is created out of band** and referenced by ARN in `cloud.json`:

```sh
aws acm request-certificate --domain-name <domain> --validation-method DNS \
    --key-algorithm RSA_2048 --profile <profile> --region us-east-1

aws acm describe-certificate --certificate-arn <arn> --profile <profile> \
    --region us-east-1 \
    --query 'Certificate.DomainValidationOptions[0].ResourceRecord'
```

Add that record at the registrar, then wait for `ISSUED`:

```sh
aws acm wait certificate-validated --certificate-arn <arn> --profile <profile> \
    --region us-east-1
```

> **Why the certificate is not a CloudFormation resource.** `AWS::CertificateManager::Certificate`
> with DNS validation blocks in `CREATE_IN_PROGRESS` until the record appears. With no
> hosted zone, CDK cannot create that record, so the deploy would sit there waiting on
> someone to open a browser — and if they took too long, roll back. An ARN in config is
> the better operational story for the one resource that genuinely has a human in the
> middle of its creation. It auto-renews for as long as the validation CNAME stays put.

### The stacks

There are **four**, and auth is two of them:

```sh
npx cdk deploy SigenData     --profile <profile>   # bucket, ingest Lambda, S3 events
npx cdk deploy SigenAuthPool --profile <profile> \
    --parameters "GoogleClientSecret=$(cat ../.google-secret)"   # Cognito + Google + callback
#   -> copy its UserPoolId and ClientId outputs into cloud.json
npx cdk deploy SigenAuthEdge --profile <profile>   # the read gate, with those ids baked in
npx cdk deploy SigenSite     --profile <profile>   # CloudFront + behaviours
```

`SigenAuthEdge` and `SigenSite` **do not appear in `cdk ls`** until
`cognito_user_pool_id` and `cognito_client_id` are in `cloud.json`. That is deliberate: a
Lambda@Edge cannot be given them as environment variables, so they are baked into its code
at synth — and synthesizing before the pool exists would bake the literal string
`${Token[...]}` and reject every valid login with no clue why. It is also why auth is two
stacks rather than one.

`SigenSite` prints the distribution domain. That is the target for the second `CNAME`
above, and it is the last manual step — after it propagates, the site is live.

### Register the redirect URI with Google — and it is not this site

`SigenAuthPool` prints two URIs, and only the first is a manual step:

| Output | Where it goes |
|---|---|
| `GoogleAuthorizedRedirectUri` | **You** paste this into the Google OAuth client's *Authorized redirect URIs*. It is `https://<auth_domain_prefix>.auth.<region>.amazoncognito.com/oauth2/idpresponse` |
| `CognitoCallbackUrl` | Nothing. The CDK sets it on the app client. It is `https://<domain>/auth/callback` |

The browser never goes from Google to this site — it goes Google → the Cognito Hosted UI →
here. Google validates `redirect_uri` against its own list, and the only URI it is ever
asked to redirect to is Cognito's `idpresponse`. Registering the site's callback instead
produces `Error 400: redirect_uri_mismatch`, in which Google echoes the `idpresponse` URI it
expected. *Authorized JavaScript origins* is unused by a redirect flow and can be empty.

Then check the consent screen's **Publishing status**. While it is *Testing*, only accounts
on its *Test users* list can sign in, whatever `allowed_emails` says — a second gate, with
its own error (`Error 403: access_denied`, "has not completed the Google verification
process"). Publishing avoids maintaining the same addresses twice, and needs no verification
review: the app requests only `openid email profile`, all non-sensitive.

`SigenAuthPool` and `SigenAuthEdge` refuse to synth with an empty `google_client_id` or an
empty `allowed_emails`. The second is the one worth stating: Cognito will authenticate *any*
Google account, so the allowlist is what actually gates the site — empty does not mean
"everyone" or "nobody", it means every visitor signs in successfully and is then refused.

### Changing the gate's code

`cdk deploy SigenAuthEdge && cdk deploy SigenSite`. That works only because
`cdk.json` sets `@aws-cdk/core:defaultCrossStackReferences` to `weak`; with CloudFormation
exports it is **impossible**. `SiteStack` takes the Lambda@Edge *version* ARN from
`SigenAuthEdge`, and the export name embeds the version's logical id, which embeds the
code's asset hash — so editing one line renames the export, and CloudFormation refuses to
delete the old one while the site imports it:

```
Cannot delete export SigenAuthEdge:ExportsOutputRef… as it is in use by SigenSite.
```

An export's value cannot be changed while it is imported, and the old value's resource is
gone, so there is nothing to keep alive. Weak references replace `Fn::ImportValue` with
`Fn::GetStackOutput`, which the CDK CLI resolves at deploy time and inlines — no export, so
the deadlock cannot recur. **Do not set this back to `strong`.**

## Cost

Well under **$1/month**. There is no Route53 hosted zone — DNS lives at the registrar
already paying for the parent domain — which removes what would otherwise have been the
single largest line item at $0.50. What is left: ~1 GB of compressed raw per year plus
its tiles (a few cents), an ingest Lambda that runs 24 times a day and sits well inside
the free tier, and CloudFront traffic for a handful of viewers, which does not register.
Cognito is free under 50 monthly active users; ACM certificates are free.

## What is deliberately not here

- **No live view.** Tiles appear when the logger rotates, so the hosted page is up to an
  hour behind and says so. `serve.py` on the LAN is the live one.
- **No field picker below a 15-hour window.** Hour tiles carry the **32** fields the panels,
  energy tiles and live strip draw. **240** — the whole **259**-field catalogue less the 6
  counters, which travel as endpoints, and the duplicate registers — is materialised from
  `b120` up, where a tile spans a day and the per-field cost collapses. Materialising
  everything at `b30` would make a tile larger than the raw `.bin.gz` it came from.

  The window that opens it is `b120`'s, and it is **15 hours, not 30**: `choose_bucket`
  rounds *up* to the next ladder width, so `b120` is chosen once `span / TARGET_BUCKETS`
  passes the width *below* it — `60 * 900 = 54,000 s`. (`120 * 900 = 108,000 s` is 30 h,
  the *top* of the `b120` band. Quoting it as the entry point is a factor of two, and both
  this file and `web/app.js` did.) None of these numbers are asserted here: `meta.json`
  carries `fine_fields`, `coarse_fields`, `catalog`, `picker_min_bucket_s` and
  `target_buckets`, and the page derives the sentence it shows from the last two.
- **No write path to the inverter, from anywhere.** Same as the rest of the repository.
