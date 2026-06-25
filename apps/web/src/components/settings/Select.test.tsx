import { describe, it, expect, vi } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import { I18nProvider } from "../../i18n";
import { Select, type SelectOption } from "./Select";

const OPTS: SelectOption[] = [
  { value: "a", label: "Alpha" },
  { value: "b", label: "Bravo" },
  { value: "c", label: "Charlie" },
];

function renderSelect(onChange = vi.fn()) {
  render(
    <I18nProvider initialLang="en">
      <Select value="" options={OPTS} onChange={onChange} placeholder="pick" />
    </I18nProvider>,
  );
  return onChange;
}

describe("Select — accessibility & keyboard", () => {
  it("exposes combobox/listbox/option roles and aria-expanded", () => {
    renderSelect();
    const combo = screen.getByRole("combobox");
    expect(combo.getAttribute("aria-expanded")).toBe("false");
    expect(combo.getAttribute("aria-haspopup")).toBe("listbox");
    act(() => { combo.click(); });
    expect(combo.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("listbox")).toBeTruthy();
    expect(screen.getAllByRole("option")).toHaveLength(3);
  });

  it("ArrowDown opens and moves the active option; Enter selects it", () => {
    const onChange = renderSelect();
    const root = screen.getByRole("combobox").parentElement as HTMLElement;
    // closed → ArrowDown opens
    fireEvent.keyDown(root, { key: "ArrowDown" });
    expect(screen.getByRole("combobox").getAttribute("aria-expanded")).toBe("true");
    // move to the 2nd option and select it
    fireEvent.keyDown(root, { key: "ArrowDown" });
    fireEvent.keyDown(root, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith("b");
  });

  it("aria-activedescendant tracks the active option", () => {
    renderSelect();
    const combo = screen.getByRole("combobox");
    act(() => { combo.click(); });
    fireEvent.keyDown(combo.parentElement as HTMLElement, { key: "End" });
    const active = combo.getAttribute("aria-activedescendant");
    const last = screen.getAllByRole("option")[2];
    expect(active).toBe(last.id);
  });

  it("Escape closes the list", () => {
    renderSelect();
    const combo = screen.getByRole("combobox");
    act(() => { combo.click(); });
    expect(screen.queryByRole("listbox")).toBeTruthy();
    fireEvent.keyDown(combo.parentElement as HTMLElement, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});
