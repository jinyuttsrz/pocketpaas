// 给 Markdown/MDX 正文里的根绝对链接补上站点 base 前缀。
// Starlight 只会为侧边栏、分页、资源等自身生成的链接加 base，
// 正文里的 [文字](/start/quickstart/) 不会自动处理，部署到子目录时会 404。
export default function remarkBaseLinks({ base = '/' } = {}) {
  const prefix = base === '/' ? '' : base.replace(/\/$/, '');

  const rewrite = (node) => {
    if (
      prefix &&
      (node.type === 'link' || node.type === 'image' || node.type === 'definition') &&
      typeof node.url === 'string' &&
      node.url.startsWith('/') &&
      !node.url.startsWith('//') &&
      node.url !== prefix &&
      !node.url.startsWith(`${prefix}/`)
    ) {
      node.url = `${prefix}${node.url}`;
    }
    if (Array.isArray(node.children)) node.children.forEach(rewrite);
  };

  return (tree) => rewrite(tree);
}
