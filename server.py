/*************************************************
 * Facebook Video Processor - Script 2
 *
 * Set a time-based trigger: every 30 minutes.
 *
 * Reads PENDING and COMPRESSING VIDEO rows from VideoLog sheet.
 *
 * Processing priority:
 *   PENDING rows:
 *     1. POST mediaUrl to /compress/start - get job_id back instantly
 *     2. Write job_id to col L, set status - COMPRESSING
 *
 *   COMPRESSING rows (next trigger run):
 *     1. GET /compress/result/<job_id>
 *        202 - still running, leave as COMPRESSING for next run
 *        200 - compressed bytes ready - post to Discord as attachment
 *        404/500 - compression failed - fall back to Vimeo (col I)
 *
 *   Discord posting priority:
 *     PRIMARY:   styled embed + compressed video attachment (- 8 MB)
 *     FALLBACK:  Vimeo URL from col I (written by Script 1)
 *     LAST RESORT: plain embed with Dropbox link
 *
 * Sheet columns:
 *   A  Timestamp
 *   B  Slug
 *   C  Type
 *   D  Title
 *   E  Description
 *   F  Facebook URL
 *   G  Media URL        - Dropbox direct-download URL
 *   H  Discord Status   - PENDING - COMPRESSING - result
 *   I  Vimeo URL        - populated by Script 1
 *   J  FB Post Date
 *   K  Feed Name
 *   L  Compress Job ID  - written here while FFmpeg runs on the worker
 *************************************************/

// ─── CONFIG ──────────────────────────────────────────────────────────────────

const SHEET_ID         = "1mqw5fJsm-ybu-pvdNUCLISYgjnbh1D4NuDk6F3TjX0k";
const YTDLP_WORKER_URL = "https://server-9tjs.onrender.com";

const VIMEO_ACCESS_TOKEN = "f64f73528b08f69583dc3fe72c2249b1";

// Discord attachment limit for this server tier.
// Non-boosted: 8 MB. Level 2: 50 MB. Level 3: 100 MB.
const DISCORD_MAX_BYTES = 8 * 1024 * 1024;

// ── Feed config ───────────────────────────────────────────────────────────────
const FEEDS = [
  {
    name:    "POP MART",
    slug:    "popmartusa",
    webhook: "https://discord.com/api/webhooks/1465602242005696681/AxYnvew2QgcYEJHl4PQUGlbEX3-EC_2YxIYwSh_ZorqP6nmyNk9Q0fBR56puAbvCpW43",
    color:   0xca001e,
    roles:   ["1466117757212033197"]
  },
  {
    name:    "Jellycat",
    slug:    "jellycatlondon",
    webhook: "https://discord.com/api/webhooks/1468102067640864911/6wdPMRu9BEXQyVORiAuZeqkAwkzuiHqeHepLPtCBqMMWUhp6mhHkYZjy19h6GESoBfnC",
    color:   0xf8a23b,
    roles:   ["1466117927584534611"]
  },
  {
    name:    "Smiski",
    slug:    "smiskiusa",
    webhook: "https://discord.com/api/webhooks/1474217622139834408/VZtM40g1UNaoLIvec_xMlXYFF47HSogr0auPu724G8uFPlIBP2arcg5EY8M-beEk3sOn",
    color:   0x9bc952,
    roles:   ["1467710899862114408", "1467384983445442706"]
  },
  {
    name:    "Cureplaneta Global",
    slug:    "cureplaneta-global",
    webhook: "https://discord.com/api/webhooks/1476409055403708477/9PxQFZz4WvVUNsxqEt4xjRYQnMnAvvJHJFKRcL76KQsS0jOkPZYyloaIWAJwuYTuWLxe",
    color:   0xf2c7c2,
    roles:   ["1466118119922864169"]
  },
  {
    name:    "Sonny Angel",
    slug:    "sonnyangel",
    webhook: "https://discord.com/api/webhooks/1468102419983503370/Gy8r761L4mQOPRjnWrj4Gx1jlitxgCRVIa4tjyzX_boM1P8mGc7OeuAOTPOlSpZwfnrw",
    color:   0xe25463,
    roles:   ["1467710936134451291", "1467384983445442706"]
  },
  {
    name:    "Sonny Angel USA",
    slug:    "sonnyangelusa",
    webhook: "https://discord.com/api/webhooks/1468102419983503370/Gy8r761L4mQOPRjnWrj4Gx1jlitxgCRVIa4tjyzX_boM1P8mGc7OeuAOTPOlSpZwfnrw",
    color:   0xe25463,
    roles:   ["1467710936134451291", "1467384983445442706"]
  }
];

// ── Column indices (0-based) ──────────────────────────────────────────────────
const COL_FEED         = 1;   // B - page slug
const COL_TYPE         = 2;   // C
const COL_TITLE        = 3;   // D
const COL_DESC         = 4;   // E
const COL_FB_URL       = 5;   // F
const COL_MEDIA        = 6;   // G - Dropbox URL
const COL_STATUS       = 7;   // H
const COL_VIMEO        = 8;   // I - Vimeo URL (written by Script 1)
const COL_FB_POST_DATE = 9;   // J
const COL_FEED_NAME    = 10;  // K
const COL_JOB_ID       = 11;  // L - compress job ID (new)

// ─── MAIN ENTRY ───────────────────────────────────────────────────────────────

/**
 * Single entry point - handles both PENDING and COMPRESSING rows.
 * Set a time trigger on this function: every 30 minutes.
 */
function processVideoQueue() {
  Logger.log("=== processVideoQueue() started ===");
  const ss    = SpreadsheetApp.openById(SHEET_ID);
  const sheet = ss.getSheetByName("VideoLog");
  if (!sheet) { Logger.log("VideoLog sheet not found."); return; }

  const data = sheet.getDataRange().getValues();
  let processed = 0;

  for (let i = 1; i < data.length && processed < 1; i++) {
    const status = (data[i][COL_STATUS] || "").toString().trim();
    const type   = (data[i][COL_TYPE]   || "").toString().trim().toUpperCase();
    if (type !== "VIDEO") continue;

    const isPending     = status === "PENDING";
    const isCompressing = status.toUpperCase() === "COMPRESSING";
    if (!isPending && !isCompressing) continue;

    const sheetRow   = i + 1;
    const feedSlug   = (data[i][COL_FEED]         || "").toString().trim();
    const feedName   = (data[i][COL_FEED_NAME]     || "").toString().trim();
    const title      = (data[i][COL_TITLE]         || "").toString().trim();
    const desc       = (data[i][COL_DESC]          || "").toString().trim();
    const fbUrl      = (data[i][COL_FB_URL]        || "").toString().trim();
    const mediaUrl   = (data[i][COL_MEDIA]         || "").toString().trim();
    const vimeoUrl   = (data[i][COL_VIMEO]         || "").toString().trim();
    const jobId      = (data[i][COL_JOB_ID]        || "").toString().trim();
    const fbPostDateRaw = data[i][COL_FB_POST_DATE];
    const fbPostDate = parseFbPostDate(fbPostDateRaw);

    Logger.log(`\nRow ${sheetRow}: [${feedSlug}] status="${status}" jobId="${jobId || "(none)"}"`);

    if (isPending) {
      processPendingRow(sheet, sheetRow, feedSlug, feedName, title, desc, fbUrl, mediaUrl, vimeoUrl, fbPostDate);
    } else {
      processCompressingRow(sheet, sheetRow, feedSlug, feedName, title, desc, fbUrl, mediaUrl, vimeoUrl, jobId, fbPostDate);
    }

    processed++;
  }

  Logger.log(`\n=== processVideoQueue() complete - processed ${processed} row(s) ===`);
}

// ─── PENDING ROW: kick off compression job ────────────────────────────────────

function processPendingRow(sheet, sheetRow, feedSlug, feedName, title, desc, fbUrl, mediaUrl, vimeoUrl, fbPostDate) {
  // Resolve source URL - prefer Dropbox, fall back to yt-dlp worker
  let sourceUrl = mediaUrl || null;
  if (!sourceUrl && YTDLP_WORKER_URL && fbUrl) {
    Logger.log(`[${feedSlug}] No media URL - trying yt-dlp worker`);
    sourceUrl = fetchVideoUrlFromWorker(fbUrl);
  }

  if (!sourceUrl) {
    Logger.log(`[${feedSlug}] No source URL - falling back to Vimeo immediately`);
    sheet.getRange(sheetRow, COL_STATUS + 1).setValue("IN PROGRESS");
    SpreadsheetApp.flush();
    const result = postVimeoOrFallback(feedSlug, feedName, title, desc, fbUrl, mediaUrl, vimeoUrl, fbPostDate);
    sheet.getRange(sheetRow, COL_STATUS + 1).setValue(result.status);
    if (result.vimeoUrl) sheet.getRange(sheetRow, COL_VIMEO + 1).setValue(result.vimeoUrl);
    SpreadsheetApp.flush();
    return;
  }

  // Fire off /compress/start - returns job_id instantly, no timeout risk
  Logger.log(`[${feedSlug}] POST /compress/start: ${sourceUrl.substring(0, 80)}...`);
  try {
    const resp = UrlFetchApp.fetch(YTDLP_WORKER_URL + "/compress/start", {
      method:      "post",
      contentType: "application/json",
      payload:     JSON.stringify({ url: sourceUrl }),
      muteHttpExceptions: true,
      followRedirects:    true
    });
    const code = resp.getResponseCode();
    Logger.log(`[${feedSlug}] /compress/start response: ${code}`);

    if (code === 200) {
      const jobId = JSON.parse(resp.getContentText()).job_id;
      Logger.log(`[${feedSlug}] Job started: ${jobId}`);
      sheet.getRange(sheetRow, COL_STATUS + 1).setValue("COMPRESSING");
      sheet.getRange(sheetRow, COL_JOB_ID + 1).setValue(jobId);
      SpreadsheetApp.flush();
    } else {
      // Worker refused to start - go straight to Vimeo fallback
      Logger.log(`[${feedSlug}] /compress/start failed (${code}) - falling back to Vimeo`);
      sheet.getRange(sheetRow, COL_STATUS + 1).setValue("IN PROGRESS");
      SpreadsheetApp.flush();
      const result = postVimeoOrFallback(feedSlug, feedName, title, desc, fbUrl, mediaUrl, vimeoUrl, fbPostDate);
      sheet.getRange(sheetRow, COL_STATUS + 1).setValue(result.status);
      if (result.vimeoUrl) sheet.getRange(sheetRow, COL_VIMEO + 1).setValue(result.vimeoUrl);
      SpreadsheetApp.flush();
    }
  } catch (err) {
    Logger.log(`[${feedSlug}] /compress/start exception: ${err}`);
    sheet.getRange(sheetRow, COL_STATUS + 1).setValue("IN PROGRESS");
    SpreadsheetApp.flush();
    const result = postVimeoOrFallback(feedSlug, feedName, title, desc, fbUrl, mediaUrl, vimeoUrl, fbPostDate);
    sheet.getRange(sheetRow, COL_STATUS + 1).setValue(result.status);
    if (result.vimeoUrl) sheet.getRange(sheetRow, COL_VIMEO + 1).setValue(result.vimeoUrl);
    SpreadsheetApp.flush();
  }
}

// ─── COMPRESSING ROW: poll for result ────────────────────────────────────────

function processCompressingRow(sheet, sheetRow, feedSlug, feedName, title, desc, fbUrl, mediaUrl, vimeoUrl, jobId, fbPostDate) {
  if (!jobId) {
    Logger.log(`[${feedSlug}] COMPRESSING but no job_id - treating as failed`);
    const result = postVimeoOrFallback(feedSlug, feedName, title, desc, fbUrl, mediaUrl, vimeoUrl, fbPostDate);
    sheet.getRange(sheetRow, COL_STATUS + 1).setValue(result.status);
    if (result.vimeoUrl) sheet.getRange(sheetRow, COL_VIMEO + 1).setValue(result.vimeoUrl);
    SpreadsheetApp.flush();
    return;
  }

  Logger.log(`[${feedSlug}] Polling /compress/result/${jobId}`);
  try {
    const resp = UrlFetchApp.fetch(`${YTDLP_WORKER_URL}/compress/result/${jobId}`, {
      method: "get",
      muteHttpExceptions: true
    });
    const code = resp.getResponseCode();
    Logger.log(`[${feedSlug}] /compress/result response: ${code}`);

    if (code === 202) {
      // Still processing - leave as COMPRESSING, try again next run
      Logger.log(`[${feedSlug}] Still compressing - will retry next run`);
      return;
    }

    if (code === 200) {
      // Compressed bytes ready
      const videoBytes = resp.getBlob().getBytes();
      Logger.log(`[${feedSlug}] Got compressed bytes: ${(videoBytes.length / 1024 / 1024).toFixed(2)} MB`);

      const feed = resolveFeed(feedSlug, feedName);
      if (!feed) {
        sheet.getRange(sheetRow, COL_STATUS + 1).setValue("FAILED - no feed match");
        SpreadsheetApp.flush();
        return;
      }

      // Clear job_id now that we have the result
      sheet.getRange(sheetRow, COL_JOB_ID + 1).setValue("");

      if (videoBytes.length <= DISCORD_MAX_BYTES) {
        const discordCode = postEmbedWithAttachment(feed, feedSlug, title, desc, fbUrl, videoBytes, fbPostDate);
        if (discordCode === 200 || discordCode === 204) {
          sheet.getRange(sheetRow, COL_STATUS + 1).setValue(`Discord ${discordCode} via embed+attachment`);
          SpreadsheetApp.flush();
          return;
        }
        Logger.log(`[${feedSlug}] Attachment failed (${discordCode}) - falling back to Vimeo`);
      } else {
        Logger.log(`[${feedSlug}] Compressed file still too large (${(videoBytes.length/1024/1024).toFixed(2)} MB) - falling back to Vimeo`);
      }

      // Compressed bytes exist but too large or Discord rejected - fall back
      const result = postVimeoOrFallback(feedSlug, feedName, title, desc, fbUrl, mediaUrl, vimeoUrl, fbPostDate);
      sheet.getRange(sheetRow, COL_STATUS + 1).setValue(result.status);
      if (result.vimeoUrl) sheet.getRange(sheetRow, COL_VIMEO + 1).setValue(result.vimeoUrl);
      SpreadsheetApp.flush();
      return;
    }

    // 404 (worker restarted) or 500 (FFmpeg error) - fall back to Vimeo
    Logger.log(`[${feedSlug}] Compression failed (${code}) - falling back to Vimeo`);
    sheet.getRange(sheetRow, COL_JOB_ID + 1).setValue("");
    const result = postVimeoOrFallback(feedSlug, feedName, title, desc, fbUrl, mediaUrl, vimeoUrl, fbPostDate);
    sheet.getRange(sheetRow, COL_STATUS + 1).setValue(result.status);
    if (result.vimeoUrl) sheet.getRange(sheetRow, COL_VIMEO + 1).setValue(result.vimeoUrl);
    SpreadsheetApp.flush();

  } catch (err) {
    Logger.log(`[${feedSlug}] /compress/result exception: ${err}`);
    sheet.getRange(sheetRow, COL_JOB_ID + 1).setValue("");
    const result = postVimeoOrFallback(feedSlug, feedName, title, desc, fbUrl, mediaUrl, vimeoUrl, fbPostDate);
    sheet.getRange(sheetRow, COL_STATUS + 1).setValue(result.status);
    if (result.vimeoUrl) sheet.getRange(sheetRow, COL_VIMEO + 1).setValue(result.vimeoUrl);
    SpreadsheetApp.flush();
  }
}

// ─── VIMEO / FALLBACK POST ────────────────────────────────────────────────────

/**
 * Shared fallback path used whenever compression is unavailable or fails.
 * Uses the Vimeo URL from col I (written by Script 1) directly - no title search.
 */
function postVimeoOrFallback(feedSlug, feedName, title, description, fbUrl, mediaUrl, vimeoUrl, fbPostDate) {
  const feed = resolveFeed(feedSlug, feedName);
  if (!feed) return { status: "FAILED - no feed match" };

  if (vimeoUrl) {
    Logger.log(`[${feedSlug}] Posting Vimeo URL`);
    const code = postEmbedWithVimeoLink(feed, feedSlug, title, description, fbUrl, vimeoUrl, fbPostDate);
    return { status: `Discord ${code} via Vimeo`, vimeoUrl };
  }

  Logger.log(`[${feedSlug}] No Vimeo URL - posting fallback embed`);
  const dropboxUrl = (mediaUrl && mediaUrl.includes("dropbox")) ? mediaUrl : null;
  const status = postFallbackEmbed(feed, feedSlug, title, description, fbUrl, dropboxUrl, fbPostDate);
  return { status };
}

// ─── FEED RESOLVER ────────────────────────────────────────────────────────────

function resolveFeed(feedSlug, feedName) {
  const feed = (feedName && FEEDS.find(f => f.name.toLowerCase() === feedName.toLowerCase()))
            || FEEDS.find(f => f.slug.toLowerCase() === feedSlug.toLowerCase());
  if (!feed) Logger.log(`WARNING: no feed match for name="${feedName}" slug="${feedSlug}"`);
  return feed || null;
}

// ─── RETRY HELPER ─────────────────────────────────────────────────────────────

function retryFailed() {
  const RETRY_STATUSES = ["failed", "in progress", "discord 204 via vimeo", "discord 200 via vimeo",
                          "discord 200 via fallback embed", "discord 204 via fallback embed"];
  const ss    = SpreadsheetApp.openById(SHEET_ID);
  const sheet = ss.getSheetByName("VideoLog");
  if (!sheet) return;
  const data = sheet.getDataRange().getValues();
  let reset = 0;
  for (let i = 1; i < data.length; i++) {
    const status = (data[i][COL_STATUS] || "").toString().trim().toLowerCase();
    const type   = (data[i][COL_TYPE]   || "").toString().trim().toUpperCase();
    if (type === "VIDEO" && RETRY_STATUSES.includes(status)) {
      sheet.getRange(i + 1, COL_STATUS + 1).setValue("PENDING");
      sheet.getRange(i + 1, COL_JOB_ID + 1).setValue("");
      Logger.log(`Row ${i + 1}: reset to PENDING`);
      reset++;
    }
  }
  SpreadsheetApp.flush();
  Logger.log(`retryFailed() complete - ${reset} row(s) reset.`);
}

// ─── TIMESTAMP HELPER ─────────────────────────────────────────────────────────

/**
 * Converts the raw FB post date value from col J into a Discord Unix timestamp.
 * Discord renders <t:UNIX:f> as a full local date+time for every user,
 * e.g. "Thursday, March 13, 2026 11:01 PM" in their own timezone.
 *
 * Falls back to a plain formatted string if the value can't be parsed.
 */
function parseFbPostDate(raw) {
  if (!raw) return "";
  try {
    const d = raw instanceof Date ? raw : new Date(raw.toString());
    if (!isNaN(d.getTime())) {
      const unix = Math.floor(d.getTime() / 1000);
      return `<t:${unix}:f>`;  // Discord relative timestamp - full date+time, user's local TZ
    }
  } catch (e) {}
  // Fallback: plain string, strip seconds  e.g. "2026-03-13 23:01"
  const s = raw.toString().trim();
  return s.replace(/(\d{4}-\d{2}-\d{2} \d{2}:\d{2}):\d{2}.*/, "$1");
}

// ─── DISCORD POSTING ─────────────────────────────────────────────────────────

function buildRolePings(feed) {
  return (feed.roles || []).map(id => `<@&${id}>`).join(" ");
}

/**
 * Builds the footer text for embed cards.
 * fbPostDate is now a Discord Unix timestamp string like "<t:1741906860:f>".
 * When it's a Unix timestamp we skip the "Facebook •" prefix because Discord
 * renders the timestamp as a full sentence on its own.
 * When it's a plain string fallback we keep the prefix.
 */
function buildFooterText(fbPostDate) {
  if (!fbPostDate) return "Facebook";
  if (fbPostDate.startsWith("<t:")) return `Facebook  •  ${fbPostDate}`;
  return `Facebook  •  ${fbPostDate}`;
}

function postEmbedWithAttachment(feed, feedSlug, title, description, fbUrl, videoBytes, fbPostDate) {
  const CRLF     = "\r\n";
  const boundary = "discordBoundary" + Utilities.getUuid().replace(/-/g, "");

  function strToBytes(str) { return Utilities.newBlob(str).getBytes(); }

  const embedBody = [
    description,
    '**' + title + '**' || null,
    `${buildRolePings(feed)}  |  [view post](<${fbUrl}>)`
  ].filter(Boolean).join("\n\n");

  const embedObj = {
    title:       `[${feed.name} (@${feedSlug})](<${fbUrl}>)`,
    description: embedBody,
    color:       feed.color,
    footer:      fbPostDate ? { text: buildFooterText(fbPostDate) } : undefined
  };

  const jsonPart = strToBytes(
    "--" + boundary + CRLF +
    'Content-Disposition: form-data; name="payload_json"' + CRLF +
    "Content-Type: application/json" + CRLF + CRLF +
    JSON.stringify({ embeds: [embedObj] }) + CRLF
  );
  const fileHeader = strToBytes(
    "--" + boundary + CRLF +
    'Content-Disposition: form-data; name="files[0]"; filename="video.mp4"' + CRLF +
    "Content-Type: video/mp4" + CRLF + CRLF
  );
  const fileFooter = strToBytes(CRLF + "--" + boundary + "--" + CRLF);

  const body = new Int8Array(jsonPart.length + fileHeader.length + videoBytes.length + fileFooter.length);
  let off = 0;
  jsonPart.forEach((b, i)   => body[off + i] = b); off += jsonPart.length;
  fileHeader.forEach((b, i) => body[off + i] = b); off += fileHeader.length;
  videoBytes.forEach((b, i) => body[off + i] = b); off += videoBytes.length;
  fileFooter.forEach((b, i) => body[off + i] = b);

  const resp = UrlFetchApp.fetch(feed.webhook, {
    method:      "post",
    contentType: `multipart/form-data; boundary=${boundary}`,
    payload:     Array.from(body),
    muteHttpExceptions: true
  });
  const code = resp.getResponseCode();
  Logger.log(`[${feedSlug}] Discord embed+attachment: ${code}`);
  return code;
}

function postEmbedWithVimeoLink(feed, feedSlug, title, description, fbUrl, vimeoUrl, fbPostDate) {
  const embedBody = [
    description,
    title || null,
    `${buildRolePings(feed)}  |  [view post](<${fbUrl}>)  |  [watch video](<${vimeoUrl}>)`,
    vimeoUrl
  ].filter(Boolean).join("\n\n");

  const embedObj = {
    title:       `[${feed.name} (@${feedSlug})](<${fbUrl}>)`,
    description: embedBody,
    color:       feed.color,
    footer:      fbPostDate ? { text: buildFooterText(fbPostDate) } : undefined
  };

  const resp = UrlFetchApp.fetch(feed.webhook, {
    method:      "post",
    contentType: "application/json",
    payload:     JSON.stringify({ embeds: [embedObj] }),
    muteHttpExceptions: true
  });
  const code = resp.getResponseCode();
  Logger.log(`[${feedSlug}] Discord Vimeo embed: ${code}`);
  return code;
}

function postFallbackEmbed(feed, feedSlug, title, description, fbUrl, dropboxUrl, fbPostDate) {
  const linkLine = dropboxUrl
    ? `[watch video](<${dropboxUrl}>)  |  [view post](<${fbUrl}>)`
    : `[view post](<${fbUrl}>)`;

  const embedObj = {
    title:       `[${feed.name} (@${feedSlug})](<${fbUrl}>)`,
    description: [description, title || null, `${buildRolePings(feed)}  |  ${linkLine}`].filter(Boolean).join("\n\n"),
    color:       feed.color,
    footer:      fbPostDate ? { text: buildFooterText(fbPostDate) } : undefined
  };

  const resp = UrlFetchApp.fetch(feed.webhook, {
    method:      "post",
    contentType: "application/json",
    payload:     JSON.stringify({ embeds: [embedObj] }),
    muteHttpExceptions: true
  });
  const code = resp.getResponseCode();
  Logger.log(`[${feedSlug}] Discord fallback embed: ${code}`);
  return `Discord ${code} via fallback embed`;
}

// ─── YT-DLP WORKER ───────────────────────────────────────────────────────────

function fetchVideoUrlFromWorker(fbUrl) {
  if (!YTDLP_WORKER_URL) return null;
  try {
    const resp = UrlFetchApp.fetch(YTDLP_WORKER_URL + "/extract", {
      method:      "post",
      contentType: "application/json",
      payload:     JSON.stringify({ url: fbUrl }),
      muteHttpExceptions: true,
      followRedirects:    true
    });
    if (resp.getResponseCode() === 200) {
      const data = JSON.parse(resp.getContentText());
      if (data.url) return data.url;
    }
    Logger.log(`fetchVideoUrlFromWorker: failed - ${resp.getContentText().substring(0, 200)}`);
    return null;
  } catch (err) {
    Logger.log(`fetchVideoUrlFromWorker: exception - ${err}`);
    return null;
  }
}

// ─── UTILITY ─────────────────────────────────────────────────────────────────

function normalizeUrl(url) {
  if (!url) return url;
  try { return decodeURIComponent(url); } catch (e) { return url; }
}
