export async function GET(request) {
  const { searchParams } = new URL(request.url);
  const query = (searchParams.get("q") || "").trim();
  const limit = searchParams.get("limit") || "8";

  if (!query) {
    return Response.json({ results: [] }, { status: 200 });
  }

  const gatewayUrl = process.env.BACKEND_URL || "http://localhost:8080";

  try {
    const response = await fetch(`${gatewayUrl}/search?q=${encodeURIComponent(query)}&limit=${limit}`, {
      method: "GET",
      cache: "no-store",
    });

    if (!response.ok) {
      return Response.json(
        { results: [], error: "Gateway query failed" },
        { status: 502 }
      );
    }

    const data = await response.json();
    return Response.json(data, { status: 200 });
  } catch (error) {
    return Response.json(
      { results: [], error: error?.message || "Search failed" },
      { status: 500 }
    );
  }
}
