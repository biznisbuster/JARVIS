import SessionsSidebar from './SessionsSidebar';
import Transcript from './Transcript';
import Composer from './Composer';
import ListenOverlay from './ListenOverlay';

export default function ChatTab() {
  return (
    <section className="chat-tab">
      <SessionsSidebar />
      <div className="chat-main">
        <Transcript />
        <Composer />
        <ListenOverlay />
      </div>
    </section>
  );
}
