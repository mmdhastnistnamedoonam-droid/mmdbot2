// ورکرِ راه‌انداز: هر ۵ دقیقه چک می‌کند ربات روی GitHub Actions روشن هست یا نه.
//  - هیچ نوبتی نیست                       → یکی راه می‌اندازد
//  - نوبتی در حالِ اجراست و عمرش زیاد شده → نوبتِ بعدی را در صف می‌گذارد (بدونِ وقفه)
//  - نوبتی در صف هست                      → کاری نمی‌کند
// مقدارها: GH_REPOSITORY ("owner/repo")، GH_REF (برنچ)، GH_PAT (سکرت)،
//          WORKFLOW_FILE، HANDOFF_MINUTES

async function gh(env, path, init = {}) {
  return fetch(`https://api.github.com${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${env.GH_PAT}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "mmd-bot-launcher",
      ...(init.headers || {}),
    },
  });
}

export async function ensureRunning(env, now = Date.now()) {
  if (!env.GH_PAT || !env.GH_REPOSITORY) {
    throw new Error("GH_PAT یا GH_REPOSITORY تنظیم نشده");
  }
  const base = `/repos/${env.GH_REPOSITORY}/actions/workflows/${env.WORKFLOW_FILE}`;
  const res = await gh(env, `${base}/runs?per_page=20`);
  if (!res.ok) {
    throw new Error(`GitHub runs: ${res.status} ${(await res.text()).slice(0, 200)}`);
  }
  const runs = (await res.json()).workflow_runs || [];
  const active = runs.filter((r) => r.status !== "completed");
  const running = active.filter((r) => r.status === "in_progress");
  const waiting = active.filter((r) => r.status !== "in_progress");

  if (waiting.length > 0) return { action: "none", reason: "next run already queued" };

  let reason = null;
  if (running.length === 0) {
    reason = "no active run";
  } else {
    const oldest = Math.min(...running.map((r) => Date.parse(r.run_started_at || r.created_at)));
    const ageMin = (now - oldest) / 60000;
    if (ageMin >= Number(env.HANDOFF_MINUTES || 300)) reason = `handoff (age ${Math.round(ageMin)}m)`;
  }
  if (!reason) return { action: "none", reason: "running fine" };

  const d = await gh(env, `${base}/dispatches`, {
    method: "POST",
    body: JSON.stringify({ ref: env.GH_REF || "main" }),
  });
  if (d.status !== 204) {
    throw new Error(`GitHub dispatch: ${d.status} ${(await d.text()).slice(0, 200)}`);
  }
  return { action: "dispatched", reason };
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(
      ensureRunning(env)
        .then((r) => console.log(JSON.stringify(r)))
        .catch((e) => console.error(String(e)))
    );
  },
};
