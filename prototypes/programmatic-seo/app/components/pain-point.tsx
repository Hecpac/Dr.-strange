const icons = ["🔴", "🟡", "🟠"];

export function PainPoint({ text, index }: { text: string; index: number }) {
  return (
    <div className="flex items-start gap-3 rounded-lg bg-gray-50 p-4">
      <span className="text-lg">{icons[index % icons.length]}</span>
      <p className="text-gray-700">{text}</p>
    </div>
  );
}
