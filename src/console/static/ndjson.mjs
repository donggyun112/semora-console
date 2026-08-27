export class NdjsonParseError extends Error {
  constructor(message, line, options = {}) {
    super(message, options);
    this.name = "NdjsonParseError";
    this.line = line;
  }
}

function parseLine(line, location) {
  try {
    return JSON.parse(line);
  } catch (cause) {
    throw new NdjsonParseError(
      `invalid NDJSON ${location}`,
      line,
      { cause },
    );
  }
}

export function createNdjsonReader(onFrame) {
  const decoder = new TextDecoder();
  let buffer = "";

  const drain = () => {
    let newline;
    while ((newline = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (line) onFrame(parseLine(line, "line"));
    }
  };

  return {
    push(bytes) {
      if (bytes?.byteLength) {
        buffer += decoder.decode(bytes, { stream: true });
      }
      drain();
    },
    end() {
      buffer += decoder.decode();
      drain();
      const tail = buffer.trim();
      buffer = "";
      if (tail) onFrame(parseLine(tail, "trailing fragment"));
    },
  };
}
