/** @type {import('next').NextConfig} */
const nextConfig = {
  basePath: "/dashboard",
  async headers() {
    return [
      {
        source: "/logos/:file*",
        headers: [{ key: "Cache-Control", value: "public, max-age=86400" }],
      },
    ];
  },
};

export default nextConfig;
