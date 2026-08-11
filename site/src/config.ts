const required = (name: string, developmentFallback: string): string => {
  const value = import.meta.env[name]?.trim();
  if (value) return value;
  if (import.meta.env.DEV) return developmentFallback;
  throw new Error(`${name} is required for a production build`);
};

const siteUrl = required("PUBLIC_SITE_URL", "http://localhost:4321").replace(/\/$/, "");

export const siteConfig = {
  name: "Moon Poro Bot",
  siteUrl,
  contactEmail: required("PUBLIC_CONTACT_EMAIL", "dev@localhost.invalid"),
  operatorName: required("PUBLIC_OPERATOR_NAME", "Moon Poro development operator"),
  operatorLocation: required("PUBLIC_OPERATOR_LOCATION", "Polska"),
  discordInviteUrl: required("PUBLIC_DISCORD_INVITE_URL", "https://discord.com/app"),
  repositoryUrl: "https://github.com/Ceendi/Moon-Poro-Bot",
  appId: "524635",
  lastUpdated: "10 sierpnia 2026",
  lastUpdatedEn: "10 August 2026",
} as const;
