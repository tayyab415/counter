import { env } from "@/config/env";
import type { Case, StaffReview } from "@/core/types";
import { clinic } from "@/data/clinic";

/**
 * Slack is the clinic's exception console. Only amber/red reviews are posted — never
 * routine conversation. Cards carry a case reference and a minimal summary and link
 * back to the controlled staff view. Full history stays in the store.
 */

export function slackEnabled() {
  return Boolean(env.slackBotToken && env.slackTriageChannel) && env.slackLive;
}

async function slackApi<T>(method: string, body: Record<string, unknown>): Promise<T> {
  const res = await fetch(`https://slack.com/api/${method}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.slackBotToken}`, "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify(body),
  });
  const json = (await res.json()) as { ok: boolean; error?: string } & T;
  if (!json.ok) throw new Error(`Slack ${method} failed: ${json.error}`);
  return json;
}

export function staffViewUrl(caseId: string) {
  const base = process.env.PUBLIC_BASE_URL || "http://localhost:3000";
  return `${base}/staff?case=${encodeURIComponent(caseId)}`;
}

/** Block Kit payload for a review card. Exported so the web staff console can render the same content. */
export function reviewBlocks(review: StaffReview, c: Case) {
  const tierEmoji = review.tier === "red" ? ":red_circle:" : ":large_orange_circle:";
  const lines = [
    `*Case:* ${review.caseId}   *Patient:* ${review.patientId}   *Ref:* ${review.id}`,
    `*Reason:*\n${review.reason}`,
    review.completed.length ? `*Agent completed:*\n${review.completed.map((s) => `✓ ${s}`).join("\n")}` : "",
    review.unresolved.length ? `*Unresolved:*\n${review.unresolved.map((s) => `• ${s}`).join("\n")}` : "",
  ].filter(Boolean);

  const blocks: Record<string, unknown>[] = [
    { type: "header", text: { type: "plain_text", text: `${review.tier === "red" ? "URGENT — " : ""}${review.title}`.slice(0, 150) } },
    { type: "section", text: { type: "mrkdwn", text: `${tierEmoji} ${lines.join("\n\n")}`.slice(0, 2900) } },
    {
      type: "context",
      elements: [
        { type: "mrkdwn", text: `Intent: ${c.intent ?? "—"} · Status: ${c.status} · <${staffViewUrl(review.caseId)}|Open in staff console>` },
      ],
    },
  ];
  if (review.actions.length && review.status === "open") {
    blocks.push({
      type: "actions",
      block_id: `review_actions_${review.id}`,
      elements: review.actions.slice(0, 5).map((a) => ({
        type: "button",
        text: { type: "plain_text", text: a.label.slice(0, 75) },
        action_id: `cp_${a.kind}`,
        value: JSON.stringify({ reviewId: review.id, actionId: a.id }),
        ...(a.kind === "confirm_appointment" ? { style: "primary" } : a.kind === "dismiss" ? { style: "danger" } : {}),
      })),
    });
  }
  return blocks;
}

export async function postReviewToSlack(review: StaffReview, c: Case): Promise<{ channel: string; ts: string } | null> {
  if (!slackEnabled()) return null;
  const res = await slackApi<{ channel: string; ts: string }>("chat.postMessage", {
    channel: env.slackTriageChannel,
    text: `[${review.caseId}] ${review.title}`,
    blocks: reviewBlocks(review, c),
    unfurl_links: false,
  });
  return { channel: res.channel, ts: res.ts };
}

export async function updateReviewInSlack(review: StaffReview, c: Case, note?: string) {
  if (!slackEnabled() || !review.slackChannel || !review.slackTs) return;
  const blocks = reviewBlocks(review, c);
  if (note) blocks.push({ type: "context", elements: [{ type: "mrkdwn", text: note }] });
  await slackApi("chat.update", { channel: review.slackChannel, ts: review.slackTs, text: `[${review.caseId}] ${review.title}`, blocks });
}

export async function postThreadReply(review: StaffReview, text: string) {
  if (!slackEnabled() || !review.slackChannel || !review.slackTs) return;
  await slackApi("chat.postMessage", { channel: review.slackChannel, thread_ts: review.slackTs, text });
}

export async function postPlainMessage(text: string, channel?: string) {
  if (!slackEnabled()) return;
  await slackApi("chat.postMessage", { channel: channel ?? env.slackTriageChannel, text });
}

export const slackChannelName = clinic.triageSlackChannel;
