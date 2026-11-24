/* ------------------------------------------------------------------ *
 *  Fahh MIDI · filename & dataset metadata parser
 *  Normalizes dataset filenames (including Composer,_First - Work [- Movement].mid)
 *  into crisp Title / Composer / Work display metadata, optional tempo cues,
 *  and leaf-title key hints.
 * ------------------------------------------------------------------ */

export type TempoCue = "largo" | "lento" | "adagio" | "andante" | "moderato" | "allegro" | "presto" | "waltz" | null;

export interface FilenameKeyHint {
  tonic: number;
  minor: boolean;
  sharps: number;
  preferredName: string;
}

export interface ParsedMidiMeta {
  name: string;
  artist?: string;
  piece?: string;
  displayTitle: string;
  displaySub: string;
  tempoCue: TempoCue;
  filenameKeyHint: FilenameKeyHint | null;
}

const KEY_WORDS: Record<string, { tonic: number; sharps: number }> = {
  c: { tonic: 0, sharps: 0 },
  "c♯": { tonic: 1, sharps: 7 },
  "d♭": { tonic: 1, sharps: -5 },
  d: { tonic: 2, sharps: 2 },
  "d♯": { tonic: 3, sharps: 6 },
  "e♭": { tonic: 3, sharps: -3 },
  e: { tonic: 4, sharps: 4 },
  f: { tonic: 5, sharps: -1 },
  "f♯": { tonic: 6, sharps: 6 },
  "g♭": { tonic: 6, sharps: -6 },
  g: { tonic: 7, sharps: 1 },
  "g♯": { tonic: 8, sharps: 5 },
  "a♭": { tonic: 8, sharps: -4 },
  a: { tonic: 9, sharps: 3 },
  "a♯": { tonic: 10, sharps: 7 },
  "b♭": { tonic: 10, sharps: -2 },
  b: { tonic: 11, sharps: 5 },
  "c♭": { tonic: 11, sharps: -7 },
};

const MAJOR_SHARPS = [0, -5, 2, -3, 4, -1, 6, 1, -4, 3, -2, 5];
const MINOR_SHARPS = [-3, 4, -1, -6, 1, -4, 3, -2, 5, 0, -5, 2];

function cleanToken(raw: string): string {
  let s = raw.replace(/_/g, " ").replace(/\s+/g, " ").trim();
  // Normalize accidental spellings in musical keys: C sharp minor -> C♯ minor, E flat major -> E♭ major
  s = s.replace(/\b([A-Ga-g])\s+sharp\b(?=\s+(?:major|minor)\b|\s*[),.;:]|$)/gi, (_, l: string) => `${l.toUpperCase()}♯`);
  s = s.replace(/\b([A-Ga-g])\s+flat\b(?=\s+(?:major|minor)\b|\s*[),.;:]|$)/gi, (_, l: string) => `${l.toUpperCase()}♭`);
  // Clean catalogue numbers such as Op.39 -> Op. 39, No.7 -> No. 7, K.310 -> K. 310, Sz.42 -> Sz. 42, D.593 -> D. 593
  s = s.replace(/\b(Op|No|Nos|K|KV|D|S|L|Sz|H|W|Hob|TH|TWV)\.(?=\d|[IVXLCDM])/gi, "$1. ");
  // Clean leading movement numbering like "2. Tango. Andantino"
  s = s.replace(/^(\d+)\.(?=[A-Za-zÀ-ÖØ-öø-ÿ])/, "$1. ");
  s = s.replace(/\s{2,}/g, " ").trim();
  return s;
}

function formatComposer(raw: string): string {
  const cleaned = cleanToken(raw);
  const comma = cleaned.indexOf(",");
  if (comma <= 0) return cleaned;
  const last = cleaned.slice(0, comma).trim();
  const first = cleaned.slice(comma + 1).trim();
  if (!first) return last;
  return `${first} ${last}`;
}

export function detectTempoCue(text: string): TempoCue {
  const low = text.toLowerCase();
  if (/\b(presto|prestissimo|vivacissimo|molto\s+allegro|allegro\s+molto|allegro\s+con\s+brio)\b/.test(low)) return "presto";
  if (/\b(waltz|valse|walzer|mazurka|minuet|menuet)\b/.test(low)) return "waltz";
  if (/\b(allegro|allegretto|vivace|scherzo|toccata|rondo|gigue|courante)\b/.test(low)) return "allegro";
  if (/\b(moderato|comodo|commodo|grazioso|tempo\s+giusto)\b/.test(low)) return "moderato";
  if (/\b(andante|andantino|cantabile)\b/.test(low)) return "andante";
  if (/\b(adagio|grave)\b/.test(low)) return "adagio";
  if (/\b(largo|larghetto|lento|assai\s+lento)\b/.test(low)) return "lento";
  return null;
}

export function extractLeafKeyHint(text: string): FilenameKeyHint | null {
  const cleaned = cleanToken(text);
  // Match key signature patterns like "in C♯ minor", "(E♭ minor)", "A minor"
  const m = cleaned.match(/\b([A-G][♯♭]?)\s+(major|minor)\b/i);
  if (!m) return null;
  const letter = m[1].charAt(0).toUpperCase() + m[1].slice(1);
  const minor = m[2].toLowerCase() === "minor";
  const info = KEY_WORDS[letter.toLowerCase()];
  if (!info) return null;
  const defaultSharps = minor ? MINOR_SHARPS[info.tonic] : MAJOR_SHARPS[info.tonic];
  // Preserve explicit enharmonic naming such as A♭ minor or E♭ minor when specified in title
  const sharps = info.sharps !== 0 && Math.abs(info.sharps) <= 7 ? info.sharps : defaultSharps;
  return {
    tonic: info.tonic,
    minor,
    sharps,
    preferredName: `${letter} ${minor ? "minor" : "major"}`,
  };
}

export function parseSongFilename(fileName: string): ParsedMidiMeta {
  const stem = fileName.replace(/\.(mid|midi)$/i, "").trim();
  const parts = stem.split(/\s+-\s+/);

  // Dataset / catalog pattern: "Composer,_First - Work [- Movement]"
  if (parts.length >= 2 && (parts[0].includes(",") || parts.length >= 3)) {
    const artist = formatComposer(parts[0]);
    if (parts.length >= 3) {
      const work = cleanToken(parts[1]);
      const movement = cleanToken(parts.slice(2).join(" — "));
      const name = `${work} — ${movement}`;
      return {
        name,
        artist,
        piece: work,
        displayTitle: movement,
        displaySub: `${artist} · ${work}`,
        tempoCue: detectTempoCue(`${movement} ${work}`),
        // Use only movement/leaf title so multi-movement sonatas (e.g. Op. 110 in Ab -> mov. 2 in F minor) aren't misled
        filenameKeyHint: extractLeafKeyHint(movement),
      };
    }
    const work = cleanToken(parts[1]);
    return {
      name: work,
      artist,
      displayTitle: work,
      displaySub: artist,
      tempoCue: detectTempoCue(work),
      filenameKeyHint: extractLeafKeyHint(work),
    };
  }

  // Generic "Artist - Title" pattern without comma
  if (parts.length === 2) {
    const artist = cleanToken(parts[0]);
    const title = cleanToken(parts[1]);
    return {
      name: title,
      artist,
      displayTitle: title,
      displaySub: artist,
      tempoCue: detectTempoCue(title),
      filenameKeyHint: extractLeafKeyHint(title),
    };
  }

  const title = cleanToken(stem || "Untitled MIDI");
  return {
    name: title,
    displayTitle: title,
    displaySub: "",
    tempoCue: detectTempoCue(title),
    filenameKeyHint: extractLeafKeyHint(title),
  };
}
