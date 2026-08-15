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
| `/d/*` (tiles) | same gate — a gate on the HTML alone would leave the data open |
| `/p/{uid}` | **public** — a shared view, copied at share time |
| `/login`, `/auth/*` | public; `/auth/callback` is what sets the cookie |

## Configuration

`cloud.json` at the repository root. **Not committed** — it holds an account id, a
domain, a Google client id and other people's email addresses, and this repository is
public. `cloud.example.json` documents every key with placeholder values and is
committed. `cdk.out/` is gitignored too: synthesized templates embed those values.

The one real secret, the Google OAuth client secret, is **never stored** — it is passed
as a `NoEcho` CloudFormation parameter and re-entered on each deploy.

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

### 1–3. DNS, with you in the middle

The site lives on a **delegated subdomain**. The parent zone stays wherever it is and
its own records — apex, `MX`, SPF, DKIM, DMARC — are never read, written or moved by
anything here. Mail cannot break, because nothing about the parent changes except one
added delegation.

```sh
npx cdk deploy SigenDns --profile <profile>          # 1. creates the zone
```

It prints four nameservers. Add them as `NS` records for the subdomain label in the
**parent** zone, at whatever registrar holds it. Change nothing else there. Then:

```sh
dig NS <domain>                                       # 2. wait for this to answer
npx cdk deploy SigenDns -c cert=1 --profile <profile> # 3. certificate
```

> **Why `-c cert=1` is a separate step.** Requesting the certificate before the
> delegation resolves does not fail — it **hangs** in `CREATE_IN_PROGRESS` until ACM
> gives up, which looks like a broken deploy rather than a wait. So the first deploy
> does not ask for it.

The hosted zone is `RemovalPolicy.RETAIN`. Deleting and recreating it issues a *new* set
of nameservers, which means editing the parent zone again — a manual step at a
registrar, triggered by a `cdk destroy` that looked routine.

### 4–6. The rest

```sh
npx cdk deploy SigenData --profile <profile>   # bucket, ingest Lambda, S3 events
npx cdk deploy SigenAuth --profile <profile>   # Cognito + Google + the edge function
npx cdk deploy SigenSite --profile <profile>   # CloudFront, behaviours, alias record
```

`SigenAuth` refuses to synth with an empty `google_client_id` or an empty
`allowed_emails`. The second is the one worth stating: Cognito will authenticate *any*
Google account, so the allowlist is what actually gates the site — empty does not mean
"everyone" or "nobody", it means every visitor signs in successfully and is then
refused.

## Cost

About **$1/month**, dominated by the Route53 hosted zone at $0.50. Storage is ~1 GB of
compressed raw per year plus its tiles; the ingest Lambda runs 24 times a day and sits
well inside the free tier; CloudFront traffic for a handful of viewers does not register.

## What is deliberately not here

- **No live view.** Tiles appear when the logger rotates, so the hosted page is up to an
  hour behind and says so. `serve.py` on the LAN is the live one.
- **No fine-grained field picker below 5-minute buckets.** Hour tiles carry the ~45
  fields the panels draw; the full ~259-field catalogue is materialised at `b300` and
  coarser, where the per-field cost collapses. Materialising everything at `b30` would
  make a tile larger than the raw `.bin.gz` it came from.
- **No write path to the inverter, from anywhere.** Same as the rest of the repository.
