import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { Sidebar } from "@/components/organisms/Sidebar";
import { getThemeInitScript } from "@/lib/theme";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Scout",
  description: "Browse and review scout scan results",
};

const themeInitScript = getThemeInitScript();

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="dark" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body
        className={`${geistSans.variable} ${geistMono.variable} bg-white font-sans text-gray-900 antialiased dark:bg-gray-950 dark:text-gray-100`}
      >
        <Sidebar />
        <main className="min-h-screen pt-14 md:ml-56 md:pt-0">
          <div className="mx-auto max-w-7xl p-4 md:p-6">{children}</div>
        </main>
      </body>
    </html>
  );
}
