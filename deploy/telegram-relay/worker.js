/**
 * Cloudflare Worker: релей к Telegram Bot API.
 *
 * 1. https://dash.cloudflare.com → Workers → Create
 * 2. Вставьте этот файл
 * 3. Settings → Variables:
 *      BOT_TOKEN     = токен от @BotFather
 *      RELAY_SECRET  = длинная случайная строка
 * 4. В Настройках сайта:
 *      URL релея     = https://<имя>.workers.dev
 *      Секрет релея  = тот же RELAY_SECRET
 */
export default {
  async fetch(request, env) {
    if (request.method === "GET") {
      return new Response("telegram relay ok", { status: 200 });
    }
    if (request.method !== "POST") {
      return Response.json({ ok: false, description: "method not allowed" }, { status: 405 });
    }
    const auth = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
    if (!env.RELAY_SECRET || auth !== env.RELAY_SECRET) {
      return Response.json({ ok: false, description: "unauthorized" }, { status: 401 });
    }
    if (!env.BOT_TOKEN) {
      return Response.json({ ok: false, description: "BOT_TOKEN is not set" }, { status: 500 });
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return Response.json({ ok: false, description: "invalid json" }, { status: 400 });
    }

    const payload = {
      chat_id: body.chat_id,
      text: body.text,
      disable_web_page_preview: true,
    };
    if (body.parse_mode) {
      payload.parse_mode = body.parse_mode;
    }

    const telegram = await fetch(
      `https://api.telegram.org/bot${env.BOT_TOKEN}/sendMessage`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      },
    );
    return new Response(await telegram.text(), {
      status: telegram.status,
      headers: { "content-type": "application/json" },
    });
  },
};
