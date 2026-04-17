import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    template: "%s | Pachano Design",
    default: "Mejores Herramientas de IA por Sector | Pachano Design",
  },
  description:
    "Descubre las mejores herramientas de inteligencia artificial para tu industria. Guías actualizadas con datos reales de mercado.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="es">
      <body className="min-h-screen bg-white text-gray-900 antialiased">
        <nav className="border-b border-gray-100 px-6 py-4">
          <div className="mx-auto flex max-w-4xl items-center justify-between">
            <a href="/" className="text-lg font-bold">
              Pachano Design
            </a>
            <a
              href="https://pachano.design"
              className="text-sm text-gray-500 hover:text-gray-900"
            >
              pachano.design
            </a>
          </div>
        </nav>
        {children}
      </body>
    </html>
  );
}
