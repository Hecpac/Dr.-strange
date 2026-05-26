from social_media_agent import ContentRequest, Platform, SocialMediaEngagementAgent


def test_agent_outputs_posts():
    agent = SocialMediaEngagementAgent()
    output = agent.create_campaign(
        ContentRequest(
            platform=Platform.INSTAGRAM,
            topic="lanzamiento de newsletter de IA para founders",
            goal="comentarios y guardados",
            audience="founders LATAM",
        )
    )
    assert len(output.posts) >= 1
    assert output.engagement_plan.comment_prompts
    assert output.safety_check["style"] == "español neutral latam, sin voseo"


if __name__ == "__main__":
    test_agent_outputs_posts()
    print("smoke test ok")
