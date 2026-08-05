/**
 * artifactStore — parses <veya-artifact type="..." title="...">...</veya-artifact> blocks out
 * of assistant chat replies (server/chat_coordinator.py's CHAT_SYSTEM_PROMPT instructs the
 * model to emit them) and tracks which one is currently docked in the split-pane
 * ArtifactRenderer.svelte.
 */

export interface Artifact {
	id: string;
	title: string;
	type: "react" | "html";
	code: string;
}

class ArtifactManager {
	activeArtifact = $state<Artifact | null>(null);

	/** Split assistant reply text into stripped prose + structured artifacts. */
	parseArtifactsFromText(text: string): { pureText: string; artifacts: Artifact[] } {
		const artifacts: Artifact[] = [];
		const regex = /<veya-artifact\s+type="([^"]+)"\s+title="([^"]+)">([\s\S]*?)<\/veya-artifact>/g;

		let pureText = text;
		let match: RegExpExecArray | null;

		while ((match = regex.exec(text)) !== null) {
			const id = crypto.randomUUID();
			artifacts.push({
				id,
				type: match[1] as "react" | "html",
				title: match[2],
				code: match[3].trim(),
			});
			pureText = pureText.replace(match[0], `\n\n[ARTIFACT_PLACEHOLDER:${id}]\n\n`);
		}

		return { pureText, artifacts };
	}

	setActive(artifact: Artifact) {
		this.activeArtifact = artifact;
	}

	close() {
		this.activeArtifact = null;
	}
}

export const artifactStore = new ArtifactManager();
