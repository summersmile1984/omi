export function authCorsOptions(allowedOrigins: string[]): {
  origin: (origin: string) => string;
  credentials: boolean;
  allowMethods: string[];
  allowHeaders: string[];
  exposeHeaders: string[];
};
