import { ComponentFixture, TestBed } from '@angular/core/testing';

import { MailAuth } from './mail-auth';

describe('MailAuth', () => {
  let component: MailAuth;
  let fixture: ComponentFixture<MailAuth>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [MailAuth],
    }).compileComponents();

    fixture = TestBed.createComponent(MailAuth);
    component = fixture.componentInstance;
    await fixture.whenStable();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
