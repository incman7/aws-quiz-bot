# AWS SAA-C03 Daily Quiz Bot — Setup Guide

Complete step-by-step instructions to get your Messenger quiz bot running on Vercel's free tier.

---

## What You'll Build

- A Flask app hosted on **Vercel** (free)
- Questions sourced from the GitHub exam dump
- Daily question at **9am AWST** via an external cron job
- AI follow-up answers powered by **Groq** (free, Llama 3.3 70B)
- Score history stored in **Vercel Postgres** (powered by Neon, free tier)

---

## Prerequisites

- A Facebook account
- A GitHub account (to push the code)
- Accounts to create (all free):
  - [vercel.com](https://vercel.com)
  - [groq.com](https://console.groq.com)
  - [cron-job.org](https://cron-job.org) (for 9am AWST trigger)

---

## Step 1 — Get a Free Groq API Key

1. Go to [console.groq.com](https://console.groq.com)
2. Sign up → **API Keys** → **Create API Key**
3. Copy the key. Save it — you'll need it later.

---

## Step 2 — Create a Facebook Page and App

### 2a. Create a Facebook Page (if you don't have one)
1. Facebook → **Pages** → **Create new Page**
2. Name it anything (e.g., "AWS Study Bot")
3. It doesn't need to be published publicly

### 2b. Create a Facebook Developer App
1. Go to [developers.facebook.com](https://developers.facebook.com)
2. Click **My Apps** → **Create App**
3. Choose **Other** → **Business** → Next
4. Enter an app name (e.g. "AWS Quiz Bot") and contact email → Create App

### 2c. Add Messenger to Your App
1. Inside your app dashboard → **Add Product** → **Messenger** → Set Up
2. Under **Access Tokens**:
   - Click **Add or Remove Pages** → select your page
   - Click **Generate Token** — copy it. This is your `PAGE_ACCESS_TOKEN`.
3. Note your **Verify Token** — use `aws-quiz-verify-token` or any string you choose.

---

## Step 3 — Push Code to GitHub

```bash
cd ~/aws-quiz-bot
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/aws-quiz-bot.git
git push -u origin main
```

---

## Step 4 — Deploy to Vercel

### 4a. Install the Vercel CLI (optional but handy)
```bash
npm i -g vercel
```

### 4b. Connect and Deploy via Vercel Dashboard

1. Go to [vercel.com](https://vercel.com) → **Add New Project**
2. Import your GitHub repository
3. Vercel will auto-detect the `vercel.json` and deploy the Flask app
4. Click **Deploy** — wait ~1 minute for the build

Your app URL will be something like `https://aws-quiz-bot.vercel.app`

### 4c. Add a Postgres Database

1. In your Vercel project dashboard → **Storage** tab → **Create Database**
2. Choose **Postgres** (powered by Neon) → select the free plan
3. Connect it to your project — Vercel will automatically add `DATABASE_URL` to your environment

### 4d. Set Environment Variables

Go to your project → **Settings** → **Environment Variables** → add:

| Key | Value |
|-----|-------|
| `PAGE_ACCESS_TOKEN` | (from Step 2c) |
| `VERIFY_TOKEN` | `aws-quiz-verify-token` (or your chosen string) |
| `GROQ_API_KEY` | (from Step 1) |
| `CRON_SECRET` | any random string, e.g. `myCronSecret123` |

> `DATABASE_URL` is added automatically when you connect the Vercel Postgres database.

5. After adding env vars, trigger a **Redeploy** so they take effect.

---

## Step 5 — Connect Facebook Webhook

1. Back in the Facebook Developer console → Messenger → **Webhooks**
2. Click **Add Callback URL**:
   - **Callback URL**: `https://aws-quiz-bot.vercel.app/webhook`
   - **Verify Token**: `aws-quiz-verify-token` (must match your env var)
3. Click **Verify and Save**
4. Under **Webhook Fields**, subscribe to: `messages`, `messaging_postbacks`
5. Under **Webhooks** → **Add Subscriptions** → select your page

### Test it
Send "hi" to your Facebook Page in Messenger. You should receive a welcome message!

---

## Step 6 — Set Up the Daily Cron Job (9am AWST)

Vercel serverless functions are stateless, so we use an external cron service to trigger the daily question.

**9am AWST = 1:00 AM UTC**

### Using cron-job.org (free)

1. Go to [cron-job.org](https://cron-job.org) → Sign up → **Create Cronjob**
2. Settings:
   - **URL**: `https://aws-quiz-bot.vercel.app/api/send-daily`
   - **Method**: POST
   - **Headers**: `X-Cron-Secret: myCronSecret123` (must match `CRON_SECRET` env var)
   - **Schedule**: 1:00 AM UTC every day (`0 1 * * *`)
3. Save

---

## Step 7 — Start Using the Bot!

Open Messenger, go to your Page, and send:

```
hi          → Welcome message + instructions
next        → Get a question right now (don't wait for 9am)
A           → Submit answer A for the current question
stats       → See your score
reset       → Reset current question state
```

After answering, you can ask **any follow-up question** like:
- "Why is SQS better than SNS here?"
- "What's the difference between SQS Standard and FIFO?"
- "When would you use Kinesis instead?"

The bot will answer using Llama 3.3 70B via Groq.

---

## Architecture Overview

```
[You in Messenger]
      ↕
[Facebook Messenger Platform]
      ↕ (HTTPS webhook)
[Vercel — Flask App (serverless)]
    ├── /webhook          → Handles messages
    ├── /api/send-daily   → Called by cron-job.org at 1am UTC (9am AWST)
    └── /api/status       → Health check
      ↕                        ↕
[Vercel Postgres (Neon)]  [Groq API — Llama 3.3]
(question history,         (follow-up explanations)
 user state)
      ↕
[GitHub — Questions.txt]
(fetched at startup,
 cached per invocation)
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "403 Forbidden" on webhook verify | Check `VERIFY_TOKEN` matches exactly |
| Bot doesn't reply | Check `PAGE_ACCESS_TOKEN` is correct; check Vercel function logs |
| No daily question arriving | Check cron-job.org is running; check `CRON_SECRET` header |
| LLM returns error | Check `GROQ_API_KEY` is valid at console.groq.com |
| "Questions not loading" | Check Vercel logs; GitHub URL may have changed |
| Database errors | Ensure `DATABASE_URL` is set (connect Vercel Postgres in the Storage tab) |

**View Vercel logs**: Dashboard → your project → **Functions** tab → click a function invocation

---

## Resetting / Starting Fresh

If you want to reset your answer history, run via the Vercel Postgres query console or any psql client:

```sql
DELETE FROM quiz_history;
DELETE FROM user_state;
```
