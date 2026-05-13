// @ts-check
import {themes as prismThemes} from 'prism-react-renderer';

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'WASP Documentation',
  tagline: 'Autonomous AI Agent Platform',
  favicon: 'img/favicon.png',

  future: {
    v4: true,
  },

  url: 'https://docs.agentwasp.com',
  baseUrl: '/',
  organizationName: 'agentwasp',
  projectName: 'wasp-docs',
  onBrokenLinks: 'warn',
  onBrokenMarkdownLinks: 'warn',


  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  markdown: {
    mermaid: true,
  },

  themes: ['@docusaurus/theme-mermaid'],

  presets: [
    [
      'classic',
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          sidebarPath: './sidebars.js',
          routeBasePath: '/',
          editUrl: undefined,
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
        gtag: {
          trackingID: 'G-TYPT8HQW5L',
          anonymizeIP: false,
        },
      }),
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      colorMode: {
        defaultMode: 'dark',
        disableSwitch: false,
        respectPrefersColorScheme: false,
      },
      navbar: {
        title: '',
        logo: {
          alt: 'WASP Agent Platform',
          src: 'img/logo.png',
          srcDark: 'img/dark_logo.png',
        },
        items: [
          {
            type: 'docSidebar',
            sidebarId: 'docs',
            position: 'left',
            label: 'Documentation',
          },
          {
            type: 'html',
            position: 'left',
            value: '<a href="/changelog" class="navbar-version-pill">v2.7</a>',
          },
          {
            href: 'https://agentwasp.com',
            label: 'Platform',
            position: 'right',
          },
        ],
      },
      footer: {
        style: 'dark',
        links: [
          {
            title: 'Docs',
            items: [
              {label: 'Getting Started', to: '/getting-started/installation'},
              {label: 'Core Concepts', to: '/core-concepts/agent-architecture'},
              {label: 'Skills Reference', to: '/core-concepts/skills'},
            ],
          },
          {
            title: 'Platform',
            items: [
              {label: 'Dashboard', href: 'https://agentwasp.com'},
            ],
          },
          {
            title: 'More',
            items: [
              {label: 'Roadmap', to: '/roadmap'},
              {label: 'Security', to: '/security/privilege-boundaries'},
            ],
          },
        ],
        copyright: `Copyright © ${new Date().getFullYear()} WASP Platform.`,
      },
      prism: {
        theme: prismThemes.github,
        darkTheme: prismThemes.vsDark,
        additionalLanguages: ['bash', 'yaml', 'python', 'json', 'nginx'],
      },
    }),
};

export default config;
