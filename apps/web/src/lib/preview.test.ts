import { describe, expect, it } from "vitest";
import { mediaKind } from "./preview";

describe("mediaKind", () => {
  it("classifies browser-native media the works preview can show in place", () => {
    expect(mediaKind("a.png")).toBe("image");
    expect(mediaKind("song.WAV")).toBe("audio"); // case-insensitive
    expect(mediaKind("track.ogg")).toBe("audio");
    expect(mediaKind("clip.mp4")).toBe("video");
    expect(mediaKind("doc.pdf")).toBe("pdf");
  });
  it("leaves text/binary as 'other' (read or download, not native preview)", () => {
    expect(mediaKind("notes.txt")).toBe("other");
    expect(mediaKind("script.py")).toBe("other");
    expect(mediaKind("page.html")).toBe("other"); // NOT inlined — XSS lane stays download
    expect(mediaKind("art.svg")).toBe("other");
    expect(mediaKind("noext")).toBe("other");
  });
});
