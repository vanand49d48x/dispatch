# SPEC — Teachloop (working name; alt brand: Talkcourse)

## What we're building
A platform where a freelancer/expert records themselves talking about a skill (or pastes YouTube links / notes)
and AI turns it into a **sellable mini-course** with a **sales page**, **Stripe checkout**, a **link-in-bio
storefront**, and an **AI tutor** trained on their content.

One-line promise: *Talk for 20 minutes, start selling tonight.*

Competitors to beat: Stan Store ($29/mo, 0% fee), Skool ($9 + 10% / $99 + 2.9%), Kajabi ($143+/mo).
None of them generate the product for you. Launch niche: freelancers teaching freelancing skills
(designers, editors, VAs, AI automation, Upwork/Fiverr growth).

## Pricing model (build billing for this)
- **Free plan:** 8% platform fee per sale (Stripe Connect `application_fee_amount`)
- **Pro plan:** $19/mo, 0% platform fee, custom domain, AI tutor, remove badge

## Stack (binding — do not substitute)
- **Next.js 15** (App Router, TypeScript, Server Actions), **Tailwind** + **shadcn/ui**, **pnpm**
- **Postgres = the Supabase project the team provisions**, accessed only through **Drizzle ORM**
  (`DATABASE_URL` pooled for runtime, `DIRECT_URL` for migrations). Enable `pgvector`.
  Do NOT use Supabase Auth, Supabase client SDK or RLS-based access; the app server is the only DB client.
- **Auth:** Better Auth (email magic link + Google)
- **Payments:** Stripe Connect Express (creator payouts) + Stripe Billing (Pro plan)
- **Video:** Mux (upload, playback, auto-captions)
- **Transcription:** Deepgram (fallback: OpenAI Whisper API)
- **LLM:** Anthropic API — `claude-sonnet-5-5` for generation, `claude-haiku-4-5-20251001` for cheap tasks.
  Deployment key: `$APP_ANTHROPIC_API_KEY` if set, otherwise `$ANTHROPIC_API_KEY`; set it in Vercel as `ANTHROPIC_API_KEY`.
- **Embeddings / RAG:** pgvector in the same Postgres (embedding provider behind an interface; mock = deterministic hash vectors)
- **Storage:** S3-compatible (Cloudflare R2) for worksheets/PDFs
- **Jobs:** Inngest (long-running generation pipeline, retries)
- **Email:** Resend
- **Deploy:** Vercel

## Providers and mocks
Every external service lives behind an interface in `/lib/providers/*` with a real and a mock implementation,
selected by env var presence (e.g. `STRIPE_SECRET_KEY` absent → `MockPayments`). Mocks must make the full
e2e flow pass:
- LLM mock: returns valid fixture JSON matching each Zod schema
- Transcription mock: fixture transcript
- Stripe mock: simulates checkout and calls the internal webhook handler
- Mux/R2 mock: local filesystem (or /tmp on Vercel)
- Email mock: logs the email and stores it in an `outbox` table viewable at `/dev/outbox` (non-production only)
- Inngest: run steps inline when `INNGEST_EVENT_KEY` is absent

## Data model (Drizzle)
```
users(id, email, name, handle UNIQUE, avatar_url, plan ENUM(free,pro), stripe_customer_id, stripe_connect_id, connect_onboarded BOOL, created_at)
sources(id, user_id, kind ENUM(recording,youtube,text,pdf), mux_asset_id, url, raw_text, transcript, status ENUM(uploaded,transcribing,ready,failed), created_at)
products(id, user_id, slug, title, subtitle, kind ENUM(course,template,community), price_cents, currency, status ENUM(draft,generating,published), sales_page JSONB, cover_url, created_at, published_at)
modules(id, product_id, position, title)
lessons(id, module_id, position, title, body_md, mux_playback_id, duration_sec, is_preview BOOL)
quizzes(id, lesson_id, questions JSONB)
assets(id, product_id, kind ENUM(worksheet,template,checklist), title, file_key)
generation_jobs(id, product_id, step, status, error, started_at, finished_at)
chunks(id, product_id, lesson_id, content, embedding VECTOR(1024))
orders(id, product_id, buyer_email, buyer_user_id NULL, amount_cents, platform_fee_cents, stripe_session_id, status, created_at)
enrollments(id, product_id, buyer_email, buyer_user_id NULL, access_token, progress JSONB, created_at)
tutor_messages(id, enrollment_id, role, content, created_at)
social_posts(id, product_id, platform ENUM(tiktok,instagram,x,linkedin), hook, body, status ENUM(draft,used))
```
(Better Auth adds its own session/account/verification tables via its Drizzle adapter.)

## Generation pipeline (Inngest functions, each step idempotent + resumable)
1. `source.transcribe` — Deepgram on uploaded audio/video or YouTube audio; store transcript.
2. `course.outline` — LLM: transcript(s) → JSON `{title, subtitle, audience, outcomes[], modules[{title, lessons[{title, summary, source_spans[]}]}]}`. Validate with Zod; retry once on parse failure.
3. `course.lessons` — fan out per lesson: write `body_md` grounded ONLY in source spans (no invented claims); mark gaps as `> TODO: creator to expand`.
4. `course.quizzes` — 3–5 MCQs per lesson.
5. `course.assets` — 1 worksheet + 1 checklist as markdown → PDF (react-pdf).
6. `sales.page` — headline, subhead, who-it's-for, outcomes, curriculum, FAQ, guarantee, CTA. Suggest price from length/niche (anchor $27–$197).
7. `tutor.index` — chunk lessons + transcript, embed into `chunks`.
8. `social.posts` — 30 short-form hooks/scripts drawn from lessons.

Progress streams to the UI by polling `generation_jobs`. All prompts live in `/lib/prompts/*.ts` as typed
functions. All LLM outputs validated with Zod.

## Routes
**Creator app (auth)**
- `/onboarding` — handle, niche, connect Stripe
- `/new` — record in browser (MediaRecorder → Mux direct upload) OR paste YouTube URLs / text / PDF
- `/products/[id]/generating` — live pipeline progress
- `/products/[id]/edit` — outline drag-to-reorder, lesson editor (markdown), quiz editor, price, sales page editor, regenerate-per-section
- `/products/[id]/promote` — social posts, copy buttons
- `/dashboard` — revenue, orders, students, tutor questions
- `/settings/billing` — Pro upgrade, Connect status

**Public**
- `/@[handle]` — link-in-bio storefront
- `/@[handle]/[slug]` — sales page + Stripe Checkout
- `/learn/[enrollmentToken]` — course player (video, lessons, quizzes, progress, downloads) + AI tutor side panel
- "Made with Teachloop" badge on the free plan
- `/` — marketing landing page for creators (promise, how it works, pricing vs Stan/Skool/Kajabi, CTA)

**API / webhooks**
- `/api/stripe/webhook` — `checkout.session.completed` → order + enrollment + email access link; `account.updated` → connect_onboarded; subscription events → plan
- `/api/mux/webhook` — asset ready → kick transcription
- `/api/inngest` — Inngest handler
- `/api/tutor` — streaming RAG answer: top-k chunks for the product, answer only from course content, cite the lesson, else say "ask the creator" and log it for the creator dashboard

## Rules
- Tutor and lesson generation stay grounded in the creator's material. No fabricated stats or claims.
- Never expose another creator's content; every query scoped by `product_id` / `user_id`.
- Platform fee = `application_fee_amount` on the Checkout Session (8% free, 0 pro).
- Buyers don't need accounts: magic access link by email; optional account claim later.
- Mobile-first for storefront, sales page and player (most traffic comes from social).
- Tests (Vitest): fee calculation, webhook handlers, outline Zod schema, tutor scoping. Playwright e2e for the happy path.
- `.env.example` lists every key; never commit secrets.
- Seed a demo creator (`demo@teachloop.dev`) with one published course so QA and the owner can click through.

## Milestones (build in order; each must run end-to-end on a deployed preview before the next)
1. **Skeleton** — Next.js + Drizzle on the Supabase Postgres + Better Auth + full schema + migrations + seed + provider interfaces with mocks + landing page.
2. **Ingest** — paste text/transcript/PDF → stored source (video comes later).
3. **Generate** — Inngest pipeline steps 2–4 → editable course in `/edit` with live progress page.
4. **Sell** — Stripe Connect onboarding, sales page generation, public sales page, Checkout with platform fee, webhook → enrollment → access email.
5. **Learn** — course player with progress, quizzes, downloads (worksheet/checklist PDFs, step 5).
6. **Tutor** — pgvector indexing + streaming tutor panel + "ask the creator" log.
7. **Video** — browser recording + Mux upload + Deepgram + YouTube import.
8. **Grow** — storefront, social posts, badge, Pro billing, creator dashboard.

## Definition of done (whole product)
Full e2e passes on the production URL (mocks allowed where keys are missing): creator signs up → pastes a
transcript → course generated → edited and published → buyer checks out → receives access email → opens
player → takes a quiz → asks the tutor a question → creator sees the order and the question on the dashboard.
README explains setup and which env vars switch each mock to the real service.
