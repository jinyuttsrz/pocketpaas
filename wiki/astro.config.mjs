import { defineConfig } from 'astro/config';
import { unified } from '@astrojs/markdown-remark';
import starlight from '@astrojs/starlight';
import tailwindcss from '@tailwindcss/vite';
import remarkBaseLinks from './src/plugins/remark-base-links.mjs';

const base = '/project/pocketpaas';

// PocketPaaS 文档站（静态构建）
// 部署在 www.jinyuttsrz.top 的子路径 /project/pocketpaas/ 下，
// 因此必须设置 base；否则资源与站内链接会以根路径 /_astro/... 请求而 404。
export default defineConfig({
  site: 'https://www.jinyuttsrz.top',
  base,
  integrations: [
    starlight({
      title: 'PocketPaaS 文档',
      description: '轻量级容器云平台 PocketPaaS 的部署、使用与接口文档',
      favicon: '/favicon.svg',
      defaultLocale: 'root',
      locales: {
        root: { label: '简体中文', lang: 'zh-CN' },
      },
      lastUpdated: true,
      pagination: true,
      pagefind: true,
      customCss: ['./src/styles/global.css'],
      head: [
        {
          tag: 'meta',
          attrs: { property: 'og:site_name', content: 'PocketPaaS 文档' },
        },
        {
          tag: 'meta',
          attrs: { name: 'theme-color', content: '#0f172a' },
        },
      ],
      sidebar: [
        { label: '开始使用', items: [
          { label: '项目简介', link: '/start/overview/' },
          { label: '快速开始', link: '/start/quickstart/' },
          { label: '本机运行', link: '/start/local/' },
        ]},
        { label: '部署', items: [
          { label: '服务器部署', link: '/deploy/server/' },
          { label: '域名与双层 Nginx', link: '/deploy/nginx/' },
          { label: '验收与故障排查', link: '/deploy/verify/' },
        ]},
        { label: '使用指南', items: [
          { label: '应用交付全流程', link: '/guide/apps/' },
          { label: '项目文件与终端', link: '/guide/files-terminal/' },
          { label: '监控、告警与审计', link: '/guide/observability/' },
          { label: 'Git Webhook 自动部署', link: '/guide/webhook/' },
        ]},
        { label: '参考', items: [
          { label: '系统架构', link: '/reference/architecture/' },
          { label: '配置项', link: '/reference/config/' },
          { label: '接口文档', collapsed: false, items: [
            { label: '总览与约定', link: '/reference/api/' },
            { label: '认证与用户', link: '/reference/api/auth-users/' },
            { label: '应用管理', link: '/reference/api/apps/' },
            { label: '源码与构建', link: '/reference/api/source-build/' },
            { label: '部署与运行观测', link: '/reference/api/runtime/' },
            { label: '文件管理', link: '/reference/api/files/' },
            { label: 'Webhook 自动部署', link: '/reference/api/webhook/' },
            { label: '告警、审计与系统', link: '/reference/api/system/' },
            { label: 'WebSocket', link: '/reference/api/websocket/' },
          ]},
        ]},
      ],
    }),
  ],
  markdown: {
    processor: unified({ remarkPlugins: [[remarkBaseLinks, { base }]] }),
  },
  vite: {
    plugins: [tailwindcss()],
  },
});
