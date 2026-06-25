import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Segmented } from "./Segmented";

const OPTS = [
  { value: "a", label: "A" },
  { value: "b", label: "B" },
  { value: "c", label: "C" },
];

describe("Segmented — radiogroup roving keyboard", () => {
  it("renders a radiogroup of radios with the selected one checked", () => {
    render(<Segmented value="b" options={OPTS} onChange={() => {}} ariaLabel="x" />);
    expect(screen.getByRole("radiogroup")).toBeTruthy();
    const radios = screen.getAllByRole("radio");
    expect(radios).toHaveLength(3);
    expect(radios[1].getAttribute("aria-checked")).toBe("true");
  });

  it("uses a roving tabindex: only the selected radio is tabbable", () => {
    render(<Segmented value="b" options={OPTS} onChange={() => {}} ariaLabel="x" />);
    const radios = screen.getAllByRole("radio");
    expect(radios.map((r) => r.getAttribute("tabindex"))).toEqual(["-1", "0", "-1"]);
  });

  it("ArrowRight/Left move AND select (wrapping)", () => {
    const onChange = vi.fn();
    render(<Segmented value="c" options={OPTS} onChange={onChange} ariaLabel="x" />);
    const group = screen.getByRole("radiogroup");
    fireEvent.keyDown(group, { key: "ArrowRight" }); // c → wraps to a
    expect(onChange).toHaveBeenLastCalledWith("a");
    fireEvent.keyDown(group, { key: "ArrowLeft" });  // c → b
    expect(onChange).toHaveBeenLastCalledWith("b");
  });

  it("Home/End jump to first/last", () => {
    const onChange = vi.fn();
    render(<Segmented value="b" options={OPTS} onChange={onChange} ariaLabel="x" />);
    const group = screen.getByRole("radiogroup");
    fireEvent.keyDown(group, { key: "Home" });
    expect(onChange).toHaveBeenLastCalledWith("a");
    fireEvent.keyDown(group, { key: "End" });
    expect(onChange).toHaveBeenLastCalledWith("c");
  });

  it("click selects", () => {
    const onChange = vi.fn();
    render(<Segmented value="a" options={OPTS} onChange={onChange} ariaLabel="x" />);
    fireEvent.click(screen.getAllByRole("radio")[2]);
    expect(onChange).toHaveBeenCalledWith("c");
  });
});
