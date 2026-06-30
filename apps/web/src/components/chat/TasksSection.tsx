/* The chara's tasks in the Profile pane: active threads it's advancing toward its
 * aspiration, plus a collapsed fold of sealed (completed) ones. Display-only — the
 * chara owns these (it sets/finishes them via the `task` tool). Pure/presentational
 * so it's unit-testable from props. */
import { useState } from "react";
import { useT } from "../../i18n";

export interface TaskItem {
  id: string;
  content: string;
  status?: string;
  done_at?: number;
}

export function TasksSection({
  tasks,
  loading,
}: {
  tasks?: { active: TaskItem[]; done: TaskItem[] };
  loading?: boolean;
}) {
  const t = useT();
  const [showDone, setShowDone] = useState(false);
  const active = tasks?.active ?? [];
  const done = tasks?.done ?? [];
  return (
    <section className="dsec">
      <h4>{t("p-tasks")}</h4>
      {loading ? (
        <div className="placeholder-pane">…</div>
      ) : active.length > 0 ? (
        <ul className="task-list">
          {active.map((it) => (
            <li key={it.id} className="task-item">
              {it.content}
            </li>
          ))}
        </ul>
      ) : (
        <div className="placeholder-pane">{t("tasks-empty")}</div>
      )}
      {done.length > 0 && (
        <div className="task-done">
          <button className="task-done-toggle" onClick={() => setShowDone((v) => !v)}>
            {showDone ? "▾" : "▸"} {t("tasks-sealed")} ({done.length})
          </button>
          {showDone && (
            <ul className="task-list done">
              {done.map((it) => (
                <li key={it.id} className="task-item done">
                  {it.content}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
