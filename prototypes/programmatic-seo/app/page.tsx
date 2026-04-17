import Link from "next/link";
import sectors from "@/data/sectors.json";

export default function Home() {
  return (
    <main className="mx-auto max-w-4xl px-6 py-16">
      <h1 className="mb-4 text-4xl font-bold tracking-tight">
        Mejores Herramientas de IA por Sector
      </h1>
      <p className="mb-12 text-lg text-gray-600">
        Guías actualizadas con datos reales de mercado, pain points y las
        herramientas que están transformando cada industria.
      </p>
      <div className="grid gap-4 sm:grid-cols-2">
        {sectors.map((s) => (
          <Link
            key={s.slug}
            href={`/sector/${s.slug}`}
            className="group rounded-xl border border-gray-200 p-6 transition hover:border-blue-300 hover:shadow-md"
          >
            <h2 className="mb-1 text-lg font-semibold group-hover:text-blue-600">
              IA para {s.sector}
            </h2>
            <p className="mb-3 text-sm text-gray-500 line-clamp-2">
              {s.description}
            </p>
            <div className="flex gap-4 text-xs text-gray-400">
              <span>Mercado: {s.market_size}</span>
              <span>+{s.growth_rate}</span>
            </div>
          </Link>
        ))}
      </div>
    </main>
  );
}
