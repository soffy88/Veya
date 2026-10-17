/**
 * Evidence Report Parser — extracts 3-tier evidence breakdown
 * (来源 Sources, 猜测 Guesses, 跳过 Skips) from bot replies.
 */

export interface ParsedEvidenceReport {
  hasEvidence: boolean;
  mainText: string;
  sources: string;
  guesses: string;
  skips: string;
}

export function parseEvidenceReport(rawText: string): ParsedEvidenceReport {
  if (!rawText || typeof rawText !== "string") {
    return { hasEvidence: false, mainText: rawText || "", sources: "", guesses: "", skips: "" };
  }

  // Common header markers: 【证据报告】 or ### 证据报告 or Evidence Report
  const headerMatch = rawText.match(/(?:【证据报告】|###\s*证据报告|#\s*证据报告|【证据三段报告】)/);
  if (!headerMatch) {
    // Also check if 来源/猜测/跳过 appear sequentially
    const seqMatch = rawText.match(/(?:1\.\s*来源|【来源】|###\s*来源).*?(?:2\.\s*猜测|【猜测】|###\s*猜测).*?(?:3\.\s*跳过|【跳过】|###\s*跳过)/s);
    if (!seqMatch) {
      return { hasEvidence: false, mainText: rawText, sources: "", guesses: "", skips: "" };
    }
  }

  let mainPart = rawText;
  let evidencePart = rawText;

  if (headerMatch && headerMatch.index !== undefined) {
    mainPart = rawText.slice(0, headerMatch.index).trim();
    evidencePart = rawText.slice(headerMatch.index + headerMatch[0].length).trim();
  }

  // Regex patterns to capture 来源, 猜测, 跳过
  // 1. 来源 (Sources)
  const sourcePattern = /(?:(?:1\.|[-*])?\s*(?:来源|Sources|数据来源)[：:]\s*)([\s\S]*?)(?=(?:(?:2\.|[-*])?\s*(?:猜测|Guesses|假设)|(?:3\.|[-*])?\s*(?:跳过|Skips)|$))/i;
  // 2. 猜测 (Guesses)
  const guessPattern = /(?:(?:2\.|[-*])?\s*(?:猜测|Guesses|假设)[：:]\s*)([\s\S]*?)(?=(?:(?:3\.|[-*])?\s*(?:跳过|Skips)|$))/i;
  // 3. 跳过 (Skips)
  const skipPattern = /(?:(?:3\.|[-*])?\s*(?:跳过|Skips|未采集)[：:]\s*)([\s\S]*?)(?=$)/i;

  const sourceMatch = evidencePart.match(sourcePattern);
  const guessMatch = evidencePart.match(guessPattern);
  const skipMatch = evidencePart.match(skipPattern);

  const sources = sourceMatch ? sourceMatch[1].trim() : "";
  const guesses = guessMatch ? guessMatch[1].trim() : "";
  const skips = skipMatch ? skipMatch[1].trim() : "";

  const hasEvidence = Boolean(sources || guesses || skips);

  return {
    hasEvidence,
    mainText: mainPart,
    sources,
    guesses,
    skips,
  };
}
