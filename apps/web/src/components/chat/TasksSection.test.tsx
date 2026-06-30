import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { I18nProvider } from "../../i18n";
import { TasksSection, type TaskItem } from "./TasksSection";

function renderSection(tasks?: { active: TaskItem[]; done: TaskItem[] }, loading = false) {
  render(
    <I18nProvider initialLang="en">
      <TasksSection tasks={tasks} loading={loading} />
    </I18nProvider>,
  );
}

const ACTIVE: TaskItem[] = [
  { id: "t1", content: "write the first chapter", status: "active" },
  { id: "t2", content: "learn the lute", status: "active" },
];
const DONE: TaskItem[] = [
  { id: "t0", content: "name the kingdom", status: "done", done_at: 1 },
];

describe("TasksSection", () => {
  it("renders active tasks as a list", () => {
    renderSection({ active: ACTIVE, done: [] });
    expect(screen.getByText("write the first chapter")).toBeTruthy();
    expect(screen.getByText("learn the lute")).toBeTruthy();
  });

  it("shows the empty placeholder when there are no active tasks", () => {
    renderSection({ active: [], done: [] });
    // the en string for tasks-empty
    expect(screen.getByText(/No tasks yet/i)).toBeTruthy();
  });

  it("shows a loading line while the fetch is in flight", () => {
    renderSection(undefined, true);
    expect(screen.getByText("…")).toBeTruthy();
  });

  it("collapses completed tasks behind a toggle, expanding on click", () => {
    renderSection({ active: ACTIVE, done: DONE });
    // the sealed task is hidden until the fold is opened
    expect(screen.queryByText("name the kingdom")).toBeNull();
    const toggle = screen.getByRole("button");
    expect(toggle.textContent).toContain("(1)"); // the done count
    fireEvent.click(toggle);
    expect(screen.getByText("name the kingdom")).toBeTruthy();
    fireEvent.click(toggle); // collapses again
    expect(screen.queryByText("name the kingdom")).toBeNull();
  });

  it("shows no fold when there are no completed tasks", () => {
    renderSection({ active: ACTIVE, done: [] });
    expect(screen.queryByRole("button")).toBeNull();
  });
});
