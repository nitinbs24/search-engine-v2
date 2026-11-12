// Unlike /api/search this is optional: any non-200 means "no answer panel", so errors are not surfaced.
export async function GET(request) {
  const { searchParams } = new URL(request.url);
  const query = (searchParams.get("q") || "").trim();
  const limit = searchParams.get("limit") || "8";
  if (!query) return Response.json({}, { status: 400 });

  const gatewayUrl = process.env.BACKEND_URL || "http://localhost:8080";
  try {
    const response = await fetch(`${gatewayUrl}/answer?q=${encodeURIComponent(query)}&limit=${limit}`, { cache: "no-store" });
    return Response.json(await response.json(), { status: response.status });
  } catch {
    return Response.json({}, { status: 502 });
  }
}
