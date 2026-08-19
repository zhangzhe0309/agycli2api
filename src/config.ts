import os from "node:os";

function generateSmartUserAgent() {
	const version = process.env.ANTIGRAVITY_VERSION || "1.1.12";
	const osPlatform = os.platform();
	const architecture = os.arch();
	const osName =
		osPlatform === "darwin"
			? "darwin"
			: osPlatform === "win32"
				? "win32"
				: "linux";
	const archName = architecture === "x64" ? "amd64" : architecture;
	return `antigravity/cli/${version} ${osName}/${archName}`;
}

export const ANTIGRAVITY_HEADERS = {
	"User-Agent": generateSmartUserAgent(),
	"Content-Type": "application/json",
	"Accept-Encoding": "gzip",
};

export const OAUTH_CONFIG = {
	clientId:
		"1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com",
	clientSecret: "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",
	tokenUrl: "https://oauth2.googleapis.com/token",
	deviceCodeUrl: "https://oauth2.googleapis.com/device/code",
};

export const ANTIGRAVITY_ENDPOINT_DAILY =
	"https://daily-cloudcode-pa.googleapis.com";

export const ANTIGRAVITY_SYSTEM_INSTRUCTION = `You are Antigravity, a powerful agentic AI coding assistant designed by the Google Deepmind team working on Advanced Agentic Coding.You are pair programming with a USER to solve their coding task. The task may require creating a new codebase, modifying or debugging an existing codebase, or simply answering a question.**Absolute paths only****Proactiveness**`;

export const TOKEN_EXPIRY_BUFFER_MS = 60000;
export const SESSION_RENEWAL_MS = 30 * 60 * 1000;
export const SESSION_EXPIRY_MS = 2 * 60 * 60 * 1000;
export const GC_INTERVAL_MS = 30 * 60 * 1000;
export const MODELS_CACHE_TTL_MS = 60 * 60 * 1000;
