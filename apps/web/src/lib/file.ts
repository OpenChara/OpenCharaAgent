/** Read a File to RAW base64 (no `data:` prefix) for the wire `data_b64` field —
 *  shared by the deck editors' upload paths. */
export function fileToB64(f: File): Promise<string> {
  return new Promise((res, rej) => {
    const reader = new FileReader();
    reader.onload = () => res(String(reader.result || "").split(",")[1] || "");
    reader.onerror = () => rej(new Error("read failed"));
    reader.readAsDataURL(f);
  });
}
