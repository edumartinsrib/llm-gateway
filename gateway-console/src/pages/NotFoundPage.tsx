import { BoxMark, FullScreen, LinkButton, NotFound, Page } from 'broto-ui';
import { ArrowLeft } from 'lucide-react';

export function NotFoundPage({ inShell = false }: { inShell?: boolean }) {
  const content = (
    <NotFound
      description="O endereço não existe ou você não tem acesso a ele."
      action={
        <LinkButton href="/" variant="primary" icon={<ArrowLeft size={16} />}>
          Voltar à visão geral
        </LinkButton>
      }
    />
  );
  if (inShell) return <Page>{content}</Page>;
  return (
    <FullScreen>
      <div className="bt-stack" style={{ alignItems: 'center' }}>
        <BoxMark size={40} />
        {content}
      </div>
    </FullScreen>
  );
}
