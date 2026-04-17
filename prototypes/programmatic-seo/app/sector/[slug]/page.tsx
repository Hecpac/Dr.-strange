import { notFound } from "next/navigation";
import { Metadata } from "next";
import sectors from "@/data/sectors.json";
import { ToolCard } from "@/app/components/tool-card";
import { PainPoint } from "@/app/components/pain-point";

interface Props {
  params: Promise<{ slug: string }>;
}

function getSector(slug: string) {
  return sectors.find((s) => s.slug === slug);
}

export async function generateStaticParams() {
  return sectors.map((s) => ({ slug: s.slug }));
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { slug } = await params;
  const sector = getSector(slug);
  if (!sector) return {};
  return {
    title: sector.headline,
    description: sector.description,
    openGraph: {
      title: sector.headline,
      description: sector.description,
      type: "article",
    },
  };
}

export default async function SectorPage({ params }: Props) {
  const { slug } = await params;
  const sector = getSector(slug);
  if (!sector) notFound();

  return (
    <main className="mx-auto max-w-4xl px-6 py-16">
      {/* Hero */}
      <header className="mb-16">
        <p className="mb-2 text-sm font-medium uppercase tracking-wider text-blue-600">
          IA para {sector.sector}
        </p>
        <h1 className="mb-4 text-4xl font-bold tracking-tight text-gray-900 sm:text-5xl">
          {sector.headline}
        </h1>
        <p className="text-lg text-gray-600">{sector.description}</p>
        <div className="mt-6 flex gap-6 text-sm text-gray-500">
          <span>
            Mercado: <strong className="text-gray-900">{sector.market_size}</strong>
          </span>
          <span>
            Crecimiento: <strong className="text-green-600">{sector.growth_rate} anual</strong>
          </span>
        </div>
      </header>

      {/* Pain Points */}
      <section className="mb-16">
        <h2 className="mb-6 text-2xl font-semibold text-gray-900">
          Problemas que la IA resuelve para {sector.sector.toLowerCase()}
        </h2>
        <div className="grid gap-4">
          {sector.pain_points.map((point, i) => (
            <PainPoint key={i} text={point} index={i} />
          ))}
        </div>
      </section>

      {/* Tools */}
      <section className="mb-16">
        <h2 className="mb-6 text-2xl font-semibold text-gray-900">
          Top herramientas de IA para {sector.sector.toLowerCase()}
        </h2>
        <div className="grid gap-6 sm:grid-cols-1 lg:grid-cols-3">
          {sector.tools.map((tool) => (
            <ToolCard key={tool.name} tool={tool} />
          ))}
        </div>
      </section>

      {/* CTA */}
      <section className="rounded-2xl bg-gray-900 px-8 py-12 text-center text-white">
        <p className="mb-4 text-xl font-medium">{sector.cta_text}</p>
        <p className="mb-6 text-gray-400">
          En Pachano Design creamos sitios web que posicionan tu negocio como
          líder en innovación.
        </p>
        <a
          href="https://pachano.design/contacto"
          className="inline-block rounded-lg bg-blue-600 px-6 py-3 font-medium text-white transition hover:bg-blue-700"
        >
          Solicitar cotización
        </a>
      </section>
    </main>
  );
}
