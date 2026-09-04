function vietnamDate(date) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Ho_Chi_Minh",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const value = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  return `${value.year}-${value.month}-${value.day}`;
}

async function run(env, now = new Date()) {
  const today = vietnamDate(now);
  const stateUrl =
    `https://raw.githubusercontent.com/${env.GITHUB_OWNER}/${env.GITHUB_REPO}` +
    `/main/state/last_sent.txt?t=${now.getTime()}`;

  const stateResponse = await fetch(stateUrl, {
    headers: { "User-Agent": "mut-bc-dong-goi-dispatcher" },
  });
  if (!stateResponse.ok) {
    throw new Error(`Khong doc duoc trang thai GitHub: ${stateResponse.status}`);
  }

  const state = await stateResponse.json();
  if (state.last_success_run_date === today) {
    console.log(JSON.stringify({ result: "stopped", reason: "already_sent", today }));
    return;
  }

  const dispatchUrl =
    `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}` +
    `/actions/workflows/${env.GITHUB_WORKFLOW}/dispatches`;
  const dispatchResponse = await fetch(dispatchUrl, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "Content-Type": "application/json",
      "User-Agent": "mut-bc-dong-goi-dispatcher",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({ ref: "main" }),
  });

  if (dispatchResponse.status !== 204) {
    throw new Error(`GitHub dispatch that bai: ${dispatchResponse.status}`);
  }
  console.log(JSON.stringify({ result: "dispatched", today }));
}

export { run, vietnamDate };

export default {
  async scheduled(_controller, env, _ctx) {
    await run(env);
  },

  async fetch(_request, _env, _ctx) {
    return Response.json({ service: "mut-bc-dong-goi-dispatcher", status: "ok" });
  },
};
