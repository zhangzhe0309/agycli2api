import crypto from "node:crypto";
import cors from "cors";
import express from "express";
import { handleGenerateContent, handleListModels } from "./proxy.js";

const app = express();
app.use(cors());
app.use(express.json({ limit: "50mb" }));

let isRandomKey = false;
if (!process.env.AGYCLI2API_KEY) {
	process.env.AGYCLI2API_KEY = crypto.randomBytes(16).toString("hex");
	isRandomKey = true;
}

// API Key Authentication Middleware
app.use((req, res, next) => {
	const expectedKey = process.env.AGYCLI2API_KEY;

	const providedKey =
		req.query.key ||
		req.headers["x-goog-api-key"] ||
		req.headers["x-api-key"] ||
		(req.headers.authorization?.startsWith("Bearer ")
			? req.headers.authorization.slice(7)
			: undefined);

	if (providedKey !== expectedKey) {
		return res
			.status(401)
			.json({ error: { message: "Unauthorized: Invalid API Key" } });
	}

	next();
});

// Standard Gemini endpoints proxy mapped to Cloud Code
app.get("/v1beta/models", handleListModels);

app.post("/v1beta/models/:modelAndAction", (req, res) => {
	const [model, action] = req.params.modelAndAction.split(":");
	const isStreaming = action === "streamGenerateContent";
	return handleGenerateContent(req, res, isStreaming, model || "");
});

const PORT = process.env.PORT || 3403;
app.listen(PORT, () => {
	console.log(`agycli2api running on http://localhost:${PORT}`);
	console.log(
		`Using credentials from ~/.gemini/antigravity-cli/antigravity-oauth-token`,
	);
	console.log(
		`API Key authentication is ENABLED. AGYCLI2API_KEY: ${process.env.AGYCLI2API_KEY}`,
	);
	if (isRandomKey) {
		console.log(
			`(The AGYCLI2API_KEY was randomly generated because it was not specified)`,
		);
	}
});
