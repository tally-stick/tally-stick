// The /api/attest that dryrun.py's synthetic scenarios answer from: the BRANCH UNDER TEST's own
// src/chain.ts attest(), run against its schema.sql through node:sqlite (the seat the society's
// attest-anchor-resolved.test.ts uses). Nothing is mocked but the transport, and the attestation()
// wrapper's prose fields (prose_revision etc.), which the witness step never reads.
//
//   node --experimental-strip-types --experimental-sqlite dryrun_attest.mjs build <tree> <db> <identityRows> [tamperId]
//   node ... dryrun_attest.mjs serve <tree> <db> <url>
//   node ... dryrun_attest.mjs row   <tree> <db> <table> <id>
//   node ... dryrun_attest.mjs page  <tree>                 print VERIFY_PAGE as the branch defines it
//
// <tree> is the checkout of the branch (a worktree), so the attest under test is the branch's.
// Live shape reproduced: identity rows 1..14 and ledger rows 1..8 are the unsealed legacy prefix
// (sealed_from_id 15 and 9 on the live chains); ledger has 11 rows as live does.
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { pathToFileURL } from "node:url";

const [cmd, tree, ...rest] = process.argv.slice(2);
const mod = (p) => import(pathToFileURL(`${tree}/${p}`).href);
const LEGACY_IDENTITY = 14, LEGACY_LEDGER = 8, LEDGER_ROWS = 11;

if (cmd === "page") {
  const { VERIFY_PAGE } = await mod("src/chain.ts");
  console.log(VERIFY_PAGE);
} else if (cmd === "build") {
  const [dbPath, n, tamper] = rest;
  const { entryHash, GENESIS } = await mod("src/chain.ts");
  const db = new DatabaseSync(dbPath);
  db.exec(readFileSync(`${tree}/schema.sql`, "utf8"));
  db.exec(`INSERT INTO citizens (id, handle, model, secret_hash, created_at, last_seen_at) VALUES (1, 'seed', 'm', 'h', 100, 100)`);
  db.exec("BEGIN");
  const insI = db.prepare(`INSERT INTO identity_events (id, citizen_id, kind, detail, created_at, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?)`);
  let prev = GENESIS;
  for (let id = 1; id <= Number(n); id++) {
    const row = { id, citizen_id: 1, kind: "joined", detail: `citizen ${id} joined`, created_at: id * 1000 };
    if (id <= LEGACY_IDENTITY) { insI.run(id, 1, row.kind, row.detail, row.created_at, null, null); continue; }
    const hash = await entryHash("identity_events", prev, row);
    insI.run(id, 1, row.kind, row.detail, row.created_at, prev, hash);
    prev = hash;
  }
  const insL = db.prepare(`INSERT INTO ledger (id, entry_date, description, amount_cents, created_at, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?)`);
  prev = GENESIS;
  for (let id = 1; id <= LEDGER_ROWS; id++) {
    const row = { id, entry_date: "2026-09-01", description: `entry ${id}`, amount_cents: 100 * id, created_at: id * 1000 };
    if (id <= LEGACY_LEDGER) { insL.run(id, row.entry_date, row.description, row.amount_cents, row.created_at, null, null); continue; }
    const hash = await entryHash("ledger", prev, row);
    insL.run(id, row.entry_date, row.description, row.amount_cents, row.created_at, prev, hash);
    prev = hash;
  }
  // an edit after sealing that leaves the stored hash in place: attest() reports it as `broken`
  if (tamper) db.prepare(`UPDATE identity_events SET detail = 'tampered' WHERE id = ?`).run(Number(tamper));
  db.exec("COMMIT");
  db.close();
  console.log(JSON.stringify({ built: dbPath, identity: Number(n), ledger: LEDGER_ROWS, tamper: tamper ? Number(tamper) : null }));
} else if (cmd === "row") {
  const [dbPath, table, id] = rest;
  const db = new DatabaseSync(dbPath, { readOnly: true });
  const r = db.prepare(`SELECT hash FROM ${table} WHERE id = ?`).get(Number(id));
  db.close();
  console.log(r?.hash ?? "");
} else if (cmd === "serve") {
  // the /api/attest route's own parameter mapping (src/index.ts): whole numbers, 64-hex expects or a 400
  const [dbPath, rawUrl] = rest;
  const { attest } = await mod("src/chain.ts");
  const { SqliteD1 } = await mod("test/helpers/sqlite-d1.ts");
  const q = new URL(rawUrl).searchParams;
  const num = (k) => (q.get(k) === null ? undefined : Number(q.get(k)));
  const str = (k) => {
    const v = q.get(k);
    if (v === null) return undefined;
    if (!/^[0-9a-f]{64}$/i.test(v)) { console.error(`400 ${k} must be a 64-char hex hash`); process.exit(22); }
    return v;
  };
  const db = new DatabaseSync(dbPath, { readOnly: true });
  const res = await attest(new SqliteD1(db), q.get("from") === null ? 0 : Number(q.get("from")), {
    identityFrom: num("identity_from"), ledgerFrom: num("ledger_from"),
    identityExpect: str("identity_expect"), ledgerExpect: str("ledger_expect"),
  });
  db.close();
  process.stdout.write(JSON.stringify(res));
} else {
  console.error("usage: page|build|row|serve"); process.exit(2);
}
