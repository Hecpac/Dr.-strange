interface Tool {
  name: string;
  category: string;
  pricing: string;
  description: string;
}

export function ToolCard({ tool }: { tool: Tool }) {
  return (
    <div className="flex flex-col rounded-xl border border-gray-200 bg-white p-6 shadow-sm transition hover:shadow-md">
      <p className="mb-1 text-xs font-medium uppercase tracking-wider text-blue-600">
        {tool.category}
      </p>
      <h3 className="mb-2 text-lg font-semibold text-gray-900">{tool.name}</h3>
      <p className="mb-4 flex-1 text-sm text-gray-600">{tool.description}</p>
      <p className="text-sm font-medium text-gray-900">{tool.pricing}</p>
    </div>
  );
}
