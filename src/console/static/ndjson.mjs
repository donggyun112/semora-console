// Byte-safe NDJSON reader for the run stream.
// Two end-of-chunk bugs this exists to close:
//   1. TextDecoder({stream:true}) holds a trailing incomplete UTF-8 sequence until
//      decode() is flushed — a Hangul syllable split across TCP packets vanished.
//   2. The last JSON line without a trailing newline was left in the buffer and dropped.

export function createNdjsonReader(onFrame) {
  const decoder = new TextDecoder();
  let buf = "";

  const drain = () => {
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) {
        try {
          onFrame(JSON.parse(line));
        } catch (_) {
          /* incomplete or non-JSON line; skip */
        }
      }
    }
  };

  return {
    push(bytes) {
      if (bytes && bytes.byteLength) buf += decoder.decode(bytes, { stream: true });
      drain();
    },
    end() {
      buf += decoder.decode();
      drain();
      const tail = buf.trim();
      buf = "";
      if (!tail) return;
      try {
        onFrame(JSON.parse(tail));
      } catch (_) {
        /* trailing junk */
      }
    },
  };
}
