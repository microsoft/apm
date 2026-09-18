import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";

export function defaultCopilotDbPath() {
    const home = process.env.COPILOT_HOME || join(homedir(), ".copilot");
    return join(home, "data.db");
}

const LIVE_SESSION_SQL = `
SELECT
  w.name AS name,
  w.session_id AS sessionId,
  COALESCE(s.is_running, 0) AS isRunning,
  w.archived_at AS archivedAt,
  w.creator_session_id AS creatorSessionId
FROM workspaces w
LEFT JOIN sessions s ON s.id = w.session_id
WHERE w.archived_at IS NULL
`;

export function readCopilotAppSessions(dbPath = defaultCopilotDbPath()) {
    if (!dbPath || !existsSync(dbPath)) return [];
    let db;
    try {
        db = new DatabaseSync(dbPath, { readOnly: true });
        const rows = db.prepare(LIVE_SESSION_SQL).all();
        return rows.map((row) => ({
            name: row.name || "",
            sessionId: row.sessionId || null,
            isRunning: Boolean(row.isRunning),
            archivedAt: row.archivedAt || null,
            creatorSessionId: row.creatorSessionId || null,
        }));
    } catch {
        return [];
    } finally {
        if (db) db.close();
    }
}
