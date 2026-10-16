/**
 * CarePlus Slack exception console — Socket Mode bot.
 *
 * Runs alongside `next dev`. Handles:
 *   • button clicks on review cards (confirm / ask patient / assign / dismiss)
 *   • "Ask patient" with a custom question via a modal
 *   • @mentions and DMs: "Why was CP-1042 escalated?", "Show cases waiting longer than two hours"
 *   • thread replies on a review card are scoped to that case automatically
 *
 * Needs: SLACK_BOT_TOKEN (xoxb-), SLACK_APP_TOKEN (xapp-, connections:write), SLACK_TRIAGE_CHANNEL.
 * Create the app from slack-app-manifest.yaml (repo root) to get the right scopes/events.
 *
 *   npx tsx scripts/slack-bot.ts
 */
import "dotenv/config";
import { App, LogLevel } from "@slack/bolt";
import { env } from "@/config/env";
import { answerStaffQuestion, performStaffAction } from "@/agent/coordinator";
import { getStore } from "@/core/store";
import { ensureSeeded } from "@/server/bootstrap";

if (!env.slackBotToken || !env.slackAppToken) {
  console.error("SLACK_BOT_TOKEN and SLACK_APP_TOKEN are required. See slack-app-manifest.yaml.");
  process.exit(1);
}

const app = new App({ token: env.slackBotToken, appToken: env.slackAppToken, socketMode: true, logLevel: LogLevel.INFO });

async function staffName(client: { users: { info: (a: { user: string }) => Promise<{ user?: { real_name?: string; name?: string } }> } }, userId: string) {
  try {
    const u = await client.users.info({ user: userId });
    return u.user?.real_name || u.user?.name || "Clinic staff";
  } catch {
    return "Clinic staff";
  }
}

/** Button clicks on review cards. action_id = cp_<kind>, value = {reviewId, actionId}. */
app.action(/^cp_/, async ({ action, ack, body, client }) => {
  await ack();
  if (action.type !== "button") return;
  const { reviewId, actionId } = JSON.parse(action.value ?? "{}") as { reviewId: string; actionId: string };
  const name = await staffName(client as never, body.user.id);
  const review = await getStore().getReview(reviewId);
  if (!review) return;
  const act = review.actions.find((a) => a.id === actionId);

  // Free-text question → open a modal so staff can type it.
  if (act?.kind === "ask_patient" && !(act.payload?.question as string | undefined) && "trigger_id" in body) {
    await client.views.open({
      trigger_id: body.trigger_id,
      view: {
        type: "modal",
        callback_id: "cp_ask_patient",
        private_metadata: JSON.stringify({ reviewId, actionId }),
        title: { type: "plain_text", text: "Ask the patient" },
        submit: { type: "plain_text", text: "Send" },
        blocks: [{ type: "input", block_id: "q", label: { type: "plain_text", text: `Question for ${review.caseId}` }, element: { type: "plain_text_input", action_id: "text", multiline: true } }],
      },
    });
    return;
  }

  const res = await performStaffAction({ reviewId, actionId, staffName: name });
  await client.chat.postMessage({ channel: body.channel?.id ?? env.slackTriageChannel, thread_ts: review.slackTs, text: `${name}: ${act?.label ?? actionId} → ${res.note}` });
});

app.view("cp_ask_patient", async ({ ack, view, body, client }) => {
  await ack();
  const { reviewId, actionId } = JSON.parse(view.private_metadata) as { reviewId: string; actionId: string };
  const q = view.state.values.q?.text?.value?.trim();
  if (!q) return;
  const name = await staffName(client as never, body.user.id);
  const res = await performStaffAction({ reviewId, actionId, staffName: name, freeText: q });
  const review = await getStore().getReview(reviewId);
  await client.chat.postMessage({ channel: env.slackTriageChannel, thread_ts: review?.slackTs, text: `${name} asked the patient: "${q}" → ${res.note}` });
});

/** Questions to the bot: mentions in channel, DMs, or thread replies under a card. */
async function handleQuestion(text: string, channel: string, threadTs: string | undefined, say: (m: { text: string; thread_ts?: string }) => Promise<unknown>) {
  const clean = text.replace(/<@[A-Z0-9]+>/g, "").trim();
  if (!clean) return;
  let caseHint: string | undefined;
  if (threadTs) {
    const reviews = await getStore().listReviews();
    caseHint = reviews.find((r) => r.slackTs === threadTs && r.slackChannel === channel)?.caseId;
  }
  const res = await answerStaffQuestion(clean, caseHint);
  await say({ text: res.answer, thread_ts: threadTs });
}

app.event("app_mention", async ({ event, say }) => {
  await handleQuestion(event.text, event.channel, event.thread_ts ?? event.ts, (m) => say(m));
});

app.message(async ({ message, say }) => {
  if (message.subtype || !("text" in message) || !message.text) return;
  const isDm = message.channel_type === "im";
  const inThread = "thread_ts" in message && Boolean(message.thread_ts);
  if (!isDm && !inThread) return; // in-channel chatter is ignored unless it is a thread on a card
  await handleQuestion(message.text, message.channel, inThread ? (message as { thread_ts?: string }).thread_ts : undefined, (m) => say(m));
});

(async () => {
  await ensureSeeded();
  await app.start();
  console.log(`⚡ CarePlus Slack bot connected (Socket Mode). Triage channel: ${env.slackTriageChannel}`);
})();
