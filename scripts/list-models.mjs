#!/usr/bin/env node

const DEFAULT_URL = "http://localhost:3403";

function printUsage() {
	console.log(`Usage: npm run list-models -- [options]

Options:
  --url <url>  API base URL or full /v1beta/models URL
  --key <key>  API key
  -h, --help   Show this help

Environment variables:
  AGYCLI2API_URL  API base URL (default: ${DEFAULT_URL})
  AGYCLI2API_KEY  API key`);
}

function readOptions(args) {
	const options = {
		url: process.env.AGYCLI2API_URL || DEFAULT_URL,
		key: process.env.AGYCLI2API_KEY,
	};

	for (let index = 0; index < args.length; index += 1) {
		const argument = args[index];
		if (argument === "-h" || argument === "--help") {
			return { ...options, help: true };
		}

		if (argument !== "--url" && argument !== "--key") {
			throw new Error(`Unknown option: ${argument}`);
		}

		const value = args[index + 1];
		if (!value || value.startsWith("--")) {
			throw new Error(`Missing value for ${argument}`);
		}

		if (argument === "--url") options.url = value;
		if (argument === "--key") options.key = value;
		index += 1;
	}

	return options;
}

function buildModelsUrl(value) {
	const url = new URL(value);
	if (!url.pathname.endsWith("/v1beta/models")) {
		url.pathname = `${url.pathname.replace(/\/$/, "")}/v1beta/models`;
	}
	return url;
}

function formatTable(models) {
	const rows = models.map((model) => [
		String(model.baseModelId || model.name?.replace(/^models\//, "") || "-"),
		String(model.displayName || "-"),
		model.outputTokenLimit == null
			? "-"
			: Number(model.outputTokenLimit).toLocaleString("en-US"),
		model.thinking === true ? "yes" : "no",
	]);
	const headers = ["MODEL", "DISPLAY NAME", "OUTPUT TOKENS", "THINKING"];
	const widths = headers.map((header, column) =>
		Math.max(header.length, ...rows.map((row) => row[column].length)),
	);
	const renderRow = (row) =>
		row.map((value, column) => value.padEnd(widths[column])).join("  ");

	return [
		renderRow(headers),
		widths.map((width) => "-".repeat(width)).join("  "),
		...rows.map(renderRow),
	].join("\n");
}

async function main() {
	const options = readOptions(process.argv.slice(2));
	if (options.help) {
		printUsage();
		return;
	}
	if (!options.key) {
		throw new Error(
			"API key is required. Set AGYCLI2API_KEY or pass --key <key>.",
		);
	}

	const url = buildModelsUrl(options.url);
	const response = await fetch(url, {
		headers: { "x-goog-api-key": options.key },
	});
	if (!response.ok) {
		const detail = (await response.text()).trim();
		throw new Error(
			`Request failed (${response.status} ${response.statusText})${detail ? `: ${detail}` : ""}`,
		);
	}

	const data = await response.json();
	if (!Array.isArray(data.models)) {
		throw new Error("Invalid response: expected a models array.");
	}
	if (data.models.length === 0) {
		console.log("No models available.");
		return;
	}

	console.log(formatTable(data.models));
	console.log(`\nTotal: ${data.models.length} models`);
}

main().catch((error) => {
	console.error(`Error: ${error.message}`);
	process.exitCode = 1;
});
