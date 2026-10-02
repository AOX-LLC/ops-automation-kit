// Print n8n credential definitions built from the kit's generated secrets.
// Output goes straight to `n8n import:credentials`, which encrypts it; it is never logged.
const fs = require("fs");

const secret = (name) => fs.readFileSync(`/run/kit-secrets/${name}`, "utf8").trim();

const credentials = [
  {
    id: "kitApiAuth000001",
    name: "Helper API (service token)",
    type: "httpHeaderAuth",
    data: { name: "Authorization", value: `Bearer ${secret("api_service_token")}` },
  },
  {
    id: "kitWebhookAuth01",
    name: "Kit webhooks (caller token)",
    type: "httpHeaderAuth",
    data: { name: "X-Kit-Token", value: secret("n8n_webhook_token") },
  },
  {
    id: "kitMailpitSmtp01",
    name: "Mailpit SMTP (local, captures all mail)",
    type: "smtp",
    data: { user: "", password: "", host: "mailpit", port: 1025, secure: false, disableStartTls: true },
  },
];

process.stdout.write(JSON.stringify(credentials));
