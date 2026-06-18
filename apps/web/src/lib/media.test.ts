import { describe, it, expect } from "vitest";
import { parseMediaLine, splitOutbound, hasMediaMarker, assetPathFor } from "./media";

describe("parseMediaLine", () => {
  it("matches a deliverable MEDIA: line", () => {
    expect(parseMediaLine("MEDIA:works/sketch.png")).toEqual({ path: "works/sketch.png", isImage: true });
    expect(parseMediaLine("  MEDIA: works/doc.pdf  ")).toEqual({ path: "works/doc.pdf", isImage: false });
    expect(parseMediaLine('`MEDIA:works/a.png`')).toEqual({ path: "works/a.png", isImage: true });
  });
  it("rejects a non-deliverable extension", () => {
    expect(parseMediaLine("MEDIA:works/main.py")).toBeNull();
  });
  it("rejects a mid-sentence MEDIA:", () => {
    expect(parseMediaLine("see MEDIA:works/a.png here")).toBeNull();
  });
  it("strips trailing quote/punctuation residue (matches the Python extractor)", () => {
    expect(parseMediaLine('MEDIA:works/a.png".')).toEqual({ path: "works/a.png", isImage: true });
    expect(parseMediaLine("MEDIA:works/a.pdf,")).toEqual({ path: "works/a.pdf", isImage: false });
  });
});

describe("splitOutbound", () => {
  it("interleaves prose and media in order, dropping the marker line from text", () => {
    const segs = splitOutbound("here it is\nMEDIA:works/a.png\nhope you like it");
    expect(segs).toEqual([
      { kind: "text", text: "here it is" },
      { kind: "media", path: "works/a.png", isImage: true },
      { kind: "text", text: "hope you like it" },
    ]);
  });
  it("coalesces consecutive prose lines into one segment", () => {
    const segs = splitOutbound("line one\nline two\nMEDIA:works/a.pdf");
    expect(segs[0]).toEqual({ kind: "text", text: "line one\nline two" });
    expect(segs[1]).toEqual({ kind: "media", path: "works/a.pdf", isImage: false });
  });
  it("plain prose is a single text segment, no media", () => {
    expect(splitOutbound("just talking")).toEqual([{ kind: "text", text: "just talking" }]);
    expect(hasMediaMarker("just talking")).toBe(false);
    expect(hasMediaMarker("x\nMEDIA:works/a.png")).toBe(true);
  });
});

describe("assetPathFor", () => {
  it("maps workspace-relative paths under the workspace root", () => {
    expect(assetPathFor("works/a.png", "/sb", "/sb/workspace")).toBe(
      "/asset?p=" + encodeURIComponent("/sb/workspace/works/a.png"),
    );
  });
  it("maps the assets/ prefix to the assets shelf", () => {
    expect(assetPathFor("assets/art.png", "/sb", "/sb/workspace")).toBe(
      "/asset?p=" + encodeURIComponent("/sb/assets/art.png"),
    );
  });
  it("passes an absolute path through (server still validates)", () => {
    expect(assetPathFor("/tmp/x.png", "/sb", "/sb/workspace")).toBe(
      "/asset?p=" + encodeURIComponent("/tmp/x.png"),
    );
  });
});
